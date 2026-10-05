"""Expired exposure and settlement recovery for both lockbot ledgers."""

import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import lockbot
from buy.lock_config import apply_defaults
from buy.lock_gates import open_exposure_usd
from buy.lock_markets import gamma_winner, parse_lock_event


SLUG = "btc-updown-5m-1791170100"
END = 1791170400.0


def market(**over):
    row = {
        "slug": SLUG, "conditionId": "condition", "outcomes": ["Up", "Down"],
        "clobTokenIds": ["up-token", "down-token"],
        "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
    }
    row.update(over)
    return parse_lock_event({"slug": SLUG, "markets": [row]})


def position(**over):
    row = {
        "slug": SLUG, "side": "down", "strategy": "s2", "end_ts": END,
        "shares": 20.0, "cost": 15.0, "fee": 0.25, "strike": None,
        "condition_id": "condition", "dry_run": True,
    }
    row.update(over)
    return row


def bot(rows, markets=None, hist=None):
    obj = lockbot.LockBot.__new__(lockbot.LockBot)
    obj.cfg = apply_defaults({})
    obj.state = {"positions": rows}
    obj.markets = markets or {}
    obj._settlement_cache = {}
    obj._lock = threading.RLock()
    obj.feeds = {"btc/usd": Mock(twap_history=Mock(return_value=hist or []))}
    obj.latched = {}
    obj.windows = {}
    obj.gamma_px = {}
    obj.session = Mock()
    obj.state_path = Mock()
    obj._risk_at = 0.0
    return obj


class ExposureTests(unittest.TestCase):
    def test_open_expired_and_settled(self):
        rows = [position(end_ts=END + 1), position(), position(settled_ts=END - 1, end_ts=END + 1)]
        self.assertEqual(open_exposure_usd(rows, END), 15)

    def test_default_clock_and_string_expiry(self):
        with patch("buy.lock_gates.time.time", return_value=END):
            self.assertEqual(open_exposure_usd([position(end_ts=str(END))]), 0)

    def test_missing_or_invalid_expiry_still_counts(self):
        self.assertEqual(open_exposure_usd([position(end_ts=None), position(end_ts="bad")], END), 30)

    def test_paper_and_live_account_share_expiry_helper(self):
        for dry in (True, False):
            with self.subTest(dry_run=dry):
                obj = bot({"old": position(dry_run=dry), "open": position(end_ts=END + 10, cost=5)})
                obj.cfg["dry_run"] = dry
                obj.cash = 100
                obj.paper = []
                obj.inflight = {"pending": {"notional": 2}}
                obj.clip_at = {}
                obj._marks_cached = Mock(return_value={})
                with patch("lockbot.open_exposure_usd", wraps=open_exposure_usd) as exposure:
                    self.assertEqual(obj._account_locked(market(), END)["open_cost"], 7)
                    exposure.assert_called_once()
                    self.assertEqual(exposure.call_args.kwargs["now"], END)


class GammaWinnerTests(unittest.TestCase):
    def test_resolved_strings_and_reversed_outcomes(self):
        row = market(umaResolutionStatus="resolved", outcomes='["Down", "Up"]', outcomePrices='["1", "0"]')
        self.assertEqual(row.resolved_winner, "down")

    def test_closed_decisive_prices(self):
        self.assertEqual(market(closed=True, outcomePrices=[1, 0]).resolved_winner, "up")

    def test_unresolved_ambiguous_and_malformed_prices_wait(self):
        for row in (
            {"outcomes": ["Up", "Down"], "outcomePrices": [1, 0]},
            {"closed": True, "outcomes": ["Up", "Down"], "outcomePrices": [.99, .01]},
            {"closed": True, "outcomes": ["Up", "Down"], "outcomePrices": "bad"},
            {"closed": True, "outcomes": ["Up", "Down"], "outcomePrices": [1]},
        ):
            with self.subTest(row=row):
                self.assertIsNone(gamma_winner(row))


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.save = patch("lockbot.atomic_save").start()
        self.log = patch("lockbot.log_event").start()
        self.fetch = patch("lockbot.fetch_market").start()
        self.addCleanup(patch.stopall)

    def test_missing_strike_with_final_falls_back_to_gamma(self):
        rows = {"down": position(), "up": position(side="up", shares=10, cost=5)}
        obj = bot(rows, {SLUG: market()}, [(END, END, 95)])
        self.fetch.return_value = market(umaResolutionStatus="resolved", outcomePrices=[0, 1])
        obj._settle_pending(END + 1)
        self.assertTrue(rows["down"]["won"])
        self.assertEqual(rows["down"]["pnl"], 4.75)
        self.assertEqual(rows["up"]["payout"], 0)
        self.assertEqual(rows["down"]["settlement_source"], "gamma")
        self.assertEqual(self.fetch.call_count, 1)
        logs = [call for call in self.log.call_args_list if call.args[0] == "settlement"]
        self.assertTrue(all(call.kwargs["settlement_source"] == "gamma" for call in logs))

    def test_twap_precedes_gamma_and_preserves_tie_rule(self):
        pos = position(side="up", strike=100)
        obj = bot({"pos": pos}, {SLUG: market(umaResolutionStatus="resolved", outcomePrices=[0, 1])}, [(END, END, 100)])
        obj._settle_pending(END + 1)
        self.assertTrue(pos["won"])
        self.assertEqual(pos["settlement_source"], "twap")
        self.fetch.assert_not_called()

    def test_gamma_price_backfills_missing_strike_for_twap(self):
        pos = position()
        obj = bot({"pos": pos}, {SLUG: market()}, [(END, END, 95)])
        self.fetch.return_value = replace(market(), price_to_beat=100)
        obj._settle_pending(END + 1)
        self.assertEqual(pos["strike"], 100)
        self.assertEqual(pos["settlement_source"], "twap")

    def test_windows_or_open_boundary_backfill_strike(self):
        for windows, hist in (({SLUG: {"strike": 100}}, [(END, END, 95)]), ({}, [(END - 300, END - 300, 100), (END, END, 95)])):
            pos = position()
            obj = bot({"pos": pos}, {SLUG: market()}, hist)
            obj.windows = windows
            obj._settle_pending(END + 1)
            self.assertEqual(pos["strike"], 100)
            self.assertEqual(pos["settlement_source"], "twap")
        self.fetch.assert_not_called()

    def test_orphan_after_restart_and_history_rolloff_settles(self):
        pos = position(settle_miss_logged=True, dry_run=False)
        obj = bot({"pos": pos})
        self.fetch.return_value = market(closed=True, outcomePrices=[0, 1])
        obj._settle_pending(END + 3600)
        self.assertEqual(pos["settlement_source"], "gamma")
        self.assertEqual(pos["settled_ts"], END + 3600)
        obj._settle_pending(END + 3601)
        self.assertEqual(self.fetch.call_count, 1)

    def test_unresolved_retry_is_throttled_and_miss_does_not_stop_it(self):
        pos = position()
        obj = bot({"pos": pos})
        self.fetch.side_effect = [market(), market(closed=True, outcomePrices=[0, 1])]
        obj._settle_pending(END + 700)
        obj._settle_pending(END + 701)
        self.assertTrue(pos["settle_miss_logged"])
        self.assertNotIn("settled_ts", pos)
        self.assertEqual(self.fetch.call_count, 1)
        obj._settle_pending(END + 720)
        self.assertEqual(pos["settlement_source"], "gamma")

    def test_fetch_failure_retries_and_can_use_ledger_context(self):
        pos = position(strike=100)
        obj = bot({"pos": pos})
        self.fetch.side_effect = RuntimeError("temporary failure")
        obj._settle_pending(END + 1)
        obj._settle_pending(END + 2)
        self.assertEqual(self.fetch.call_count, 1)
        obj.feeds["btc/usd"].twap_history.return_value = [(END, END, 95)]
        obj._settle_pending(END + 3)
        self.assertEqual(pos["settlement_source"], "twap")

    def test_future_and_settled_positions_are_skipped(self):
        obj = bot({"future": position(end_ts=END + 50), "settled": position(settled_ts=END)})
        obj._settle_pending(END + 1)
        self.fetch.assert_not_called()
        self.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
