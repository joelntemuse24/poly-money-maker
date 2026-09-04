"""Last-print vs PTB trading gates. No RTDS / no bot import."""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from buy.btc_price import (
    BtcUnderlyingFeed,
    LIVE_KIND_LAST_PRINT,
    LIVE_KIND_TWAP,
    SOURCE_BINANCE,
    SOURCE_CHAINLINK,
    SOURCE_TWAP_30,
    SOURCE_TWAP_60,
    is_last_print_source,
    require_last_print_source,
    side_from_live_vs_ptb,
)
from buy.complement_gate import evaluate_complement, oracle_favors_other_leg
from buy.hedge_gate import hedge_oracle_allows_sell

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "buybot.py"
BOT5M = ROOT / "buybot5m.py"
BOT_HR = ROOT / "buybothourly.py"
COMPLEMENT = ROOT / "complementbot.py"


def _seed_open_and_live(feed: BtcUnderlyingFeed, start_ts: float, ptb: float, live: float) -> None:
    """Historical open tick for PTB, then a fresh last-print for live_quote."""
    ok = feed._push_tick(int(start_ts * 1000), ptb, live=False)
    if not ok:
        raise AssertionError("failed to push PTB tick")
    rec = feed.capture_ptb(start_ts)
    if not rec or not rec.get("ok"):
        raise AssertionError(f"PTB capture failed: {rec}")
    ok = feed._push_tick(int(time.time() * 1000), live, live=True)
    if not ok:
        raise AssertionError("failed to push live tick")


class LastPrintSourceGuards(unittest.TestCase):
    def test_chainlink_and_binance_are_last_print(self):
        self.assertTrue(is_last_print_source(SOURCE_CHAINLINK))
        self.assertTrue(is_last_print_source(SOURCE_BINANCE))
        self.assertFalse(is_last_print_source(SOURCE_TWAP_30))
        self.assertFalse(is_last_print_source(SOURCE_TWAP_60))
        self.assertEqual(require_last_print_source(SOURCE_CHAINLINK), SOURCE_CHAINLINK)
        self.assertEqual(require_last_print_source(SOURCE_BINANCE), SOURCE_BINANCE)

    def test_twap_is_not_a_trading_source(self):
        with self.assertRaisesRegex(ValueError, "last-print vs PTB"):
            require_last_print_source(SOURCE_TWAP_60)
        with self.assertRaisesRegex(ValueError, "last-print vs PTB"):
            require_last_print_source(SOURCE_TWAP_30)


class SideFromLiveVsPtb(unittest.TestCase):
    def test_up_and_down_from_last_vs_open(self):
        up = side_from_live_vs_ptb(100_040.0, 100_000.0, 0.0)
        self.assertTrue(up["ok"])
        self.assertEqual(up["favored"], "up")
        down = side_from_live_vs_ptb(99_960.0, 100_000.0, 0.0)
        self.assertTrue(down["ok"])
        self.assertEqual(down["favored"], "down")

    def test_flat_never_picks_a_side(self):
        flat = side_from_live_vs_ptb(100_000.0, 100_000.0, 0.0)
        self.assertFalse(flat["ok"])
        self.assertIsNone(flat["favored"])
        self.assertEqual(flat["reason"], "edge_zero")


class UnderlyingCheckLastPrint(unittest.TestCase):
    def test_favored_side_follows_last_print_not_a_lagging_average(self):
        start = time.time() - 45.0
        ptb = 100_000.0
        feed = BtcUnderlyingFeed(SOURCE_CHAINLINK, "")
        _seed_open_and_live(feed, start, ptb, ptb - 80.0)
        chk = feed.underlying_check(start, 0.0)
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["favored"], "down")
        self.assertEqual(chk["live_kind"], LIVE_KIND_LAST_PRINT)
        self.assertEqual(chk["live_source"], "chainlink_btc_usd")
        live, src, age = feed.live_quote()
        self.assertAlmostEqual(live, ptb - 80.0)
        self.assertEqual(src, "chainlink_btc_usd")
        self.assertIsNotNone(age)
        self.assertLess(age, 5.0)

    def test_twap_feed_is_refused_as_live_even_when_ticks_exist(self):
        start = time.time() - 45.0
        ptb = 100_000.0
        twap = BtcUnderlyingFeed(SOURCE_TWAP_60, "")
        _seed_open_and_live(twap, start, ptb, ptb + 5.0)
        chk = twap.underlying_check(start, 0.0)
        self.assertFalse(chk["ok"])
        self.assertIsNone(chk["favored"])
        self.assertEqual(chk["reason"], "twap_not_live")
        self.assertEqual(chk["live_kind"], LIVE_KIND_TWAP)
        # live_quote still exposes the average for logging, but the gate
        # must not treat it as last print.
        live, src, _age = twap.live_quote()
        self.assertAlmostEqual(live, ptb + 5.0)
        self.assertEqual(src, "chainlink_twap_60s")

    def test_binance_hourly_last_print_still_gates(self):
        start = time.time() - 20.0
        feed = BtcUnderlyingFeed(SOURCE_BINANCE, "")
        _seed_open_and_live(feed, start, 110_000.0, 110_025.0)
        chk = feed.underlying_check(start, 0.0)
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["favored"], "up")
        self.assertEqual(chk["live_kind"], LIVE_KIND_LAST_PRINT)


class LaggingTwapWouldSkipLastPrintFires(unittest.TestCase):
    """2026-09-02 15m 1788329700: Down 86¢ with ~16s left, TWAP still Up."""

    def setUp(self):
        self.start = time.time() - 30.0
        self.ptb = 100_000.0
        # Last print has already crossed down. 60s TWAP has not.
        self.live_last = self.ptb - 40.0
        self.lagging_twap = self.ptb + 12.0

    def test_complement_fires_on_last_print_cross(self):
        live_feed = BtcUnderlyingFeed(SOURCE_CHAINLINK, "")
        _seed_open_and_live(live_feed, self.start, self.ptb, self.live_last)
        chk = live_feed.underlying_check(self.start, 0.0)
        self.assertTrue(oracle_favors_other_leg(chk, "down"))
        fire, why, shares = evaluate_complement(
            other_ask=0.86,
            other_bid=0.84,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=oracle_favors_other_leg(chk, "down"),
            spend_cap=16.0,
            share_cap=20.0,
            share_multiple=1.0,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "fire")
        self.assertGreaterEqual(shares, 10.0)

        twap_side = side_from_live_vs_ptb(self.lagging_twap, self.ptb, 0.0)
        self.assertEqual(twap_side["favored"], "up")
        self.assertFalse(oracle_favors_other_leg(twap_side, "down"))
        # 86¢ other-leg ask is the trigger even if a lagging print still
        # says Up. The live bot no longer skips that as oracle_still_held.
        fire_lag, why_lag, _ = evaluate_complement(
            other_ask=0.86,
            other_bid=0.84,
            held_shares=10.87,
            already_bought=False,
            primary_still_holding=True,
            oracle_favors_other=oracle_favors_other_leg(twap_side, "down"),
        )
        self.assertTrue(fire_lag)
        self.assertEqual(why_lag, "fire")

    def test_hedge_require_oracle_follows_last_print(self):
        live_feed = BtcUnderlyingFeed(SOURCE_CHAINLINK, "")
        _seed_open_and_live(live_feed, self.start, self.ptb, self.live_last)
        chk = live_feed.underlying_check(self.start, 0.0)
        allow, why = hedge_oracle_allows_sell("up", chk)
        self.assertTrue(allow)
        self.assertEqual(why, "oracle_against")

        twap_chk = {
            "ok": True,
            "favored": "up",
            "edge_usd": 12.0,
            "live_kind": LIVE_KIND_TWAP,
        }
        hold, hold_why = hedge_oracle_allows_sell("up", twap_chk)
        self.assertFalse(hold)
        self.assertEqual(hold_why, "oracle_still_winning")


class BotsWireLastPrintOracle(unittest.TestCase):
    def test_live_and_stopped_bots_do_not_treat_twap_as_live(self):
        fifteen = BOT.read_text()
        five = BOT5M.read_text()
        hourly = BOT_HR.read_text()
        complement = COMPLEMENT.read_text()
        for src, name in (
            (fifteen, "buybot.py"),
            (five, "buybot5m.py"),
            (hourly, "buybothourly.py"),
            (complement, "complementbot.py"),
        ):
            self.assertNotIn("SOURCE_TWAP_30", src, name)
            self.assertNotIn("SOURCE_TWAP_60", src, name)
            self.assertNotIn("ptb_twap", src, name)
            self.assertIn("require_last_print_source", src, name)
            self.assertIn("get_btc_feed", src, name)

        self.assertIn("SOURCE_CHAINLINK", fifteen)
        self.assertIn("SOURCE_CHAINLINK", five)
        self.assertIn("SOURCE_CHAINLINK", complement)
        self.assertIn("ptb_chainlink_buy.json", fifteen)
        self.assertIn("ptb_chainlink_buy5m.json", five)
        self.assertIn("ptb_chainlink_buy.json", complement)
        self.assertIn("ptb_chainlink_buy5m.json", complement)
        self.assertIn("SOURCE_BINANCE", hourly)
        self.assertIn("ptb_binance_buyhourly.json", hourly)
        self.assertIn("oracle_favors_other_leg", complement)


if __name__ == "__main__":
    unittest.main()
