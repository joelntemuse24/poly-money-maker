"""Sister scrap-bidder policy. No CLOB, no mintbot import."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

from buy.sister_bid import (
    COMPLEMENT_DEPOSIT,
    MINTBOT_FUNDER,
    SISTER_DEFAULTS,
    a_token_flat,
    buy_matched_shares,
    plan_sister_bids,
    sister_quote,
    resolve_sister_client_config,
    sister_cancel_due,
    sister_funder_ok,
    sister_miss_events,
    sister_poll_s,
)


ROOT = Path(__file__).resolve().parents[1]


def _market(**overrides):
    base = {
        "condition_id": "cid",
        "slug": "btc-updown-15m-1790100900",
        "up_token": "UP",
        "dn_token": "DN",
        "end_ts": 10_000.0,
        "up_bid": 0.02,
        "dn_bid": 0.97,
    }
    base.update(overrides)
    return base


class FlatAfterATests(unittest.TestCase):
    def test_no_bid_while_a_still_long_loser(self):
        now = 10_000.0 - 120.0
        intent = {
            "status": "confirmed",
            "sold_loser": False,
            "sell_loser_leg": "up",
            "shares": 50,
        }
        actions = plan_sister_bids(
            markets=[_market()],
            intents={"cid": intent},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        self.assertFalse(any(row["op"] == "place" for row in actions))
        self.assertTrue(any(row["reason"] == "a_still_long" for row in actions))
        flat, why = a_token_flat(intent, "up")
        self.assertFalse(flat)
        self.assertEqual(why, "a_still_long")

    def test_sold_loser_with_live_rest_still_places(self):
        now = 10_000.0 - 100.0
        intent = {
            "status": "confirmed",
            "sold_loser": True,
            "sold_leg": "up",
            "sell_loser_leg": "up",
            "sell_scrap_rest_id": "rest-1",
            "end_ts": 10_000.0,
        }
        flat, why = a_token_flat(intent, "up")
        self.assertTrue(flat)
        self.assertEqual(why, "a_flat")
        actions = plan_sister_bids(
            markets=[_market()],
            intents={"cid": intent},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["leg"], "up")
        self.assertEqual(places[0]["reason"], "a_flat")
        held = [row for row in actions if row["leg"] == "dn"]
        self.assertTrue(all(row["op"] == "skip" for row in held))
        self.assertTrue(any(row["reason"] == "a_holds_other" for row in held))

    def test_unsold_rest_still_blocks(self):
        intent = {
            "status": "confirmed",
            "sold_loser": False,
            "sell_loser_leg": "up",
            "sell_scrap_rest_id": "rest-1",
        }
        flat, why = a_token_flat(intent, "up")
        self.assertFalse(flat)
        self.assertEqual(why, "a_rest_live")

    def test_places_20_after_a_is_flat_on_loser(self):
        now = 10_000.0 - 100.0
        intent = {
            "status": "confirmed",
            "sold_loser": True,
            "sold_leg": "up",
            "sell_loser_leg": "up",
        }
        actions = plan_sister_bids(
            markets=[_market(up_bid=None, dn_bid=0.99)],
            intents={"cid": intent},
            open_orders={},
            now_s=now,
            shares=SISTER_DEFAULTS["shares"],
            bid_max_px=SISTER_DEFAULTS["bid_max_px"],
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["leg"], "up")
        self.assertEqual(places[0]["shares"], 20.0)
        self.assertEqual(places[0]["price"], 0.04)
        self.assertEqual(places[0]["reason"], "a_flat")
        self.assertIn(places[0]["tif"], ("GTD", "GTC"))
        held = [row for row in actions if row["leg"] == "dn"]
        self.assertTrue(any(row["reason"] == "a_holds_other" for row in held))

    def test_absent_market_bids_the_cheap_live_book(self):
        now = 10_000.0 - 90.0
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.02, dn_bid=0.96)],
            intents={},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["leg"], "up")
        self.assertEqual(places[0]["reason"], "a_absent")
        self.assertEqual(places[0]["shares"], 20.0)

    def test_absent_market_skips_when_neither_side_is_cheap(self):
        now = 10_000.0 - 90.0
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.40, dn_bid=0.60)],
            intents={},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        self.assertFalse(any(row["op"] == "place" for row in actions))
        self.assertTrue(all(row["reason"] == "not_cheap" for row in actions))


class CancelAndWindowTests(unittest.TestCase):
    def test_cancel_before_expiry(self):
        due, why = sister_cancel_due(ttm_s=15.0, cancel_ttm_s=20.0, window_open=True)
        self.assertTrue(due)
        self.assertEqual(why, "cancel_before_expiry")
        due, why = sister_cancel_due(ttm_s=0.0, cancel_ttm_s=20.0, window_open=False)
        self.assertEqual(why, "window_end")
        now = 10_000.0 - 15.0
        actions = plan_sister_bids(
            markets=[_market()],
            intents={
                "cid": {
                    "status": "confirmed",
                    "sold_loser": True,
                    "sold_leg": "up",
                }
            },
            open_orders={"cid": {"up": {"order_id": "oid-9"}}},
            now_s=now,
            enabled=True,
        )
        cancels = [row for row in actions if row["op"] == "cancel"]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["order_id"], "oid-9")
        self.assertEqual(cancels[0]["reason"], "cancel_before_expiry")

    def test_too_early_does_not_bid(self):
        now = 10_000.0 - 200.0
        actions = plan_sister_bids(
            markets=[_market()],
            intents={},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        self.assertTrue(all(row["reason"] == "too_early" for row in actions))

    def test_post_scrap_places_before_active_window(self):
        now = 10_000.0 - 400.0
        intent = {
            "status": "confirmed",
            "sold_loser": True,
            "sold_leg": "up",
            "end_ts": 10_000.0,
        }
        actions = plan_sister_bids(
            markets=[_market()],
            intents={"cid": intent},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual([row["leg"] for row in places], ["up"])
        self.assertEqual(places[0]["reason"], "a_flat")
        self.assertGreater(places[0]["ttm_s"], 180.0)
        held = [row for row in actions if row["leg"] == "dn"]
        self.assertTrue(any(row["reason"] == "a_holds_other" for row in held))
        self.assertFalse(any(row["op"] == "place" and row["leg"] == "dn" for row in actions))
        absent = plan_sister_bids(
            markets=[_market()],
            intents={},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        self.assertTrue(all(row["reason"] == "too_early" for row in absent))
        self.assertFalse(any(row["op"] == "place" for row in absent))

    def test_disabled_cancels_open_and_places_nothing(self):
        now = 10_000.0 - 100.0
        actions = plan_sister_bids(
            markets=[_market()],
            intents={},
            open_orders={"cid": {"up": {"order_id": "oid"}}},
            now_s=now,
            enabled=False,
        )
        self.assertTrue(any(row["op"] == "cancel" and row["order_id"] == "oid" for row in actions))
        self.assertFalse(any(row["op"] == "place" for row in actions))


class SisterAuthTests(unittest.TestCase):
    def test_defaults_are_20_shares_at_four_cents(self):
        self.assertEqual(SISTER_DEFAULTS["shares"], 20.0)
        self.assertEqual(SISTER_DEFAULTS["bid_max_px"], 0.04)
        self.assertIs(SISTER_DEFAULTS["bid_enabled"], False)
        self.assertIs(SISTER_DEFAULTS["dry_run"], True)
        self.assertEqual(SISTER_DEFAULTS["active_ttm_s"], 180.0)
        self.assertEqual(SISTER_DEFAULTS["cancel_ttm_s"], 20.0)
        self.assertEqual(SISTER_DEFAULTS["poll_hot_s"], 1.0)
        self.assertEqual(SISTER_DEFAULTS["miss_after_s"], 10.0)
        self.assertEqual(SISTER_DEFAULTS["miss_throttle_s"], 30.0)
        self.assertIs(SISTER_DEFAULTS["bid_take_enabled"], True)
        example = json.loads(
            (ROOT / "strategy_scrapbid.example.json").read_text(encoding="utf-8")
        )
        self.assertIs(example["bid_take_enabled"], True)
        self.assertEqual(example["bid_max_px"], 0.04)
        self.assertEqual(example["shares"], 20.0)

    def test_poly_1271_and_deposit_funder_are_the_default(self):
        self.assertEqual(int(SignatureTypeV2.POLY_1271), 3)
        cfg = resolve_sister_client_config({})
        self.assertEqual(cfg["signature_type"], 3)
        self.assertEqual(cfg["funder"], COMPLEMENT_DEPOSIT)
        self.assertNotIn("key", cfg)
        self.assertNotIn("private_key", cfg)

    def test_refuses_mintbot_funder(self):
        ok, why = sister_funder_ok(MINTBOT_FUNDER)
        self.assertFalse(ok)
        self.assertEqual(why, "mintbot_funder")
        with self.assertRaises(ValueError):
            resolve_sister_client_config({"FUNDER_ADDRESS": MINTBOT_FUNDER})

    def test_runner_does_not_import_mintbot_or_sell(self):
        src = (ROOT / "scrapbidder.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertNotIn("mintbot", imported)
        self.assertIn(".env.complement", src)
        self.assertNotIn("load_dotenv()", src)
        self.assertIn("BUY", src)
        self.assertNotIn("side=SELL", src)
        self.assertIn("OrderType.FAK", src)
        self.assertIn("bid_take_enabled", src)
        self.assertIn("_fetch_top", src)
        self.assertNotIn("build_atomic_mint", src)
        self.assertNotIn("submit_mint", src)
        self.assertIn("scrapbid_miss", src)
        self.assertIn("sister_poll_s", src)
        self.assertIn("poll_hot_s", src)


class PostScrapMissTests(unittest.TestCase):
    def _intent(self, **extra):
        row = {
            "status": "confirmed",
            "sold_loser": True,
            "sold_leg": "up",
            "slug": "btc-updown-15m-1790100900",
            "end_ts": 10_000.0,
        }
        row.update(extra)
        return row

    def test_miss_fires_once_when_delayed_then_throttles(self):
        intent = self._intent()
        events, flat_at, emit_at = sister_miss_events(
            intents={"cid": intent},
            open_orders={},
            now_s=1_000.0,
            first_flat_at={},
            last_emit_at={},
        )
        self.assertEqual(events, [])
        self.assertEqual(flat_at["cid:up"], 1_000.0)
        events, flat_at, emit_at = sister_miss_events(
            intents={"cid": intent},
            open_orders={},
            now_s=1_009.0,
            first_flat_at=flat_at,
            last_emit_at=emit_at,
        )
        self.assertEqual(events, [])
        events, flat_at, emit_at = sister_miss_events(
            intents={"cid": intent},
            open_orders={},
            now_s=1_010.0,
            first_flat_at=flat_at,
            last_emit_at=emit_at,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "scrapbid_miss")
        self.assertEqual(events[0]["condition_id"], "cid")
        self.assertEqual(events[0]["leg"], "up")
        self.assertAlmostEqual(events[0]["age_s"], 10.0)
        self.assertAlmostEqual(events[0]["ttm"], 9_000.0 - 10.0)
        events, flat_at, emit_at = sister_miss_events(
            intents={"cid": intent},
            open_orders={},
            now_s=1_020.0,
            first_flat_at=flat_at,
            last_emit_at=emit_at,
        )
        self.assertEqual(events, [])
        events, _flat_at, _emit_at = sister_miss_events(
            intents={"cid": intent},
            open_orders={},
            now_s=1_040.0,
            first_flat_at=flat_at,
            last_emit_at=emit_at,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["leg"], "up")

    def test_open_bid_or_winner_or_cancel_window_is_not_a_miss(self):
        intent = self._intent(last_sell_attempt_at=1_000.0)
        events, _flat, _emit = sister_miss_events(
            intents={"cid": intent},
            open_orders={"cid": {"up": {"order_id": "bid-1"}}},
            now_s=1_030.0,
        )
        self.assertEqual(events, [])
        events, _flat, _emit = sister_miss_events(
            intents={"cid": intent},
            open_orders={},
            now_s=9_990.0,
        )
        self.assertEqual(events, [])
        events, _flat, _emit = sister_miss_events(
            intents={"cid": self._intent(sold_leg="dn", sell_loser_leg="dn")},
            open_orders={},
            now_s=1_030.0,
            first_flat_at={},
        )
        self.assertEqual([row["leg"] for row in events], [])
        # dn is the sold leg; with last attempt absent the clock starts now.
        events, flat_at, _emit = sister_miss_events(
            intents={"cid": self._intent(sold_leg="dn")},
            open_orders={},
            now_s=1_000.0,
        )
        self.assertEqual(events, [])
        self.assertIn("cid:dn", flat_at)
        self.assertNotIn("cid:up", flat_at)

    def test_attempt_clock_makes_the_first_look_a_miss(self):
        events, _flat, _emit = sister_miss_events(
            intents={"cid": self._intent(last_sell_attempt_at=980.0)},
            open_orders={},
            now_s=1_000.0,
        )
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["age_s"], 20.0)

    def test_hot_poll_while_sold_leg_has_no_bid(self):
        intent = self._intent()
        hot = sister_poll_s(
            intents={"cid": intent},
            open_orders={},
            now_s=9_000.0,
            poll_s=2.0,
            hot_poll_s=1.0,
            enabled=True,
        )
        self.assertEqual(hot, 1.0)
        idle = sister_poll_s(
            intents={"cid": intent},
            open_orders={"cid": {"up": {"order_id": "bid-1"}}},
            now_s=9_000.0,
            poll_s=2.0,
            hot_poll_s=1.0,
            enabled=True,
        )
        self.assertEqual(idle, 2.0)
        disabled = sister_poll_s(
            intents={"cid": intent},
            open_orders={},
            now_s=9_000.0,
            poll_s=2.0,
            hot_poll_s=1.0,
            enabled=False,
        )
        self.assertEqual(disabled, 2.0)


def _sold_up():
    return {
        "status": "confirmed",
        "sold_loser": True,
        "sold_leg": "up",
    }


def _up_places(market, **kwargs):
    actions = plan_sister_bids(
        markets=[market],
        intents={"cid": _sold_up()},
        open_orders={},
        now_s=10_000.0 - 100.0,
        enabled=True,
        **kwargs,
    )
    return [row for row in actions if row["op"] == "place" and row["leg"] == "up"]


class PostScrapPriceTests(unittest.TestCase):
    def test_takes_two_cent_ask_after_sold(self):
        places = _up_places(_market(up_bid=0.01, up_ask=0.02, dn_bid=0.99))
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["style"], "take")
        self.assertEqual(places[0]["tif"], "FAK")
        self.assertEqual(places[0]["price"], 0.02)
        self.assertEqual(places[0]["shares"], 20.0)
        self.assertEqual(places[0]["price_why"], "live_ask")
        self.assertEqual(places[0]["reason"], "a_flat")

    def test_rests_at_three_cent_bid_when_ask_is_rich(self):
        places = _up_places(_market(up_bid=0.03, up_ask=None, dn_bid=0.99))
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["price"], 0.03)
        self.assertEqual(places[0]["price_why"], "join_bid")
        self.assertNotEqual(places[0]["tif"], "FAK")
        places = _up_places(_market(up_bid=0.03, up_ask=0.05, dn_bid=0.99))
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["price"], 0.03)
        self.assertNotEqual(places[0]["tif"], "FAK")

    def test_never_pays_above_four_cents(self):
        places = _up_places(_market(up_bid=0.05, up_ask=0.06, dn_bid=0.99))
        self.assertEqual(places[0]["price"], 0.04)
        self.assertEqual(places[0]["style"], "rest")
        self.assertLessEqual(places[0]["price"], 0.04)
        places = _up_places(_market(up_bid=0.05, up_ask=0.04, dn_bid=0.99))
        self.assertEqual(places[0]["style"], "take")
        self.assertEqual(places[0]["price"], 0.04)
        style, price, why = sister_quote(bid=0.09, ask=0.08, bid_max_px=0.04)
        self.assertEqual((style, price, why), ("rest", 0.04, "cap"))

    def test_rich_ask_rests_at_cap_instead_of_taking(self):
        places = _up_places(_market(up_bid=None, up_ask=0.05, dn_bid=0.99))
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["tif"] != "FAK", True)
        self.assertEqual(places[0]["price"], 0.04)
        self.assertEqual(places[0]["price_why"], "cap")

    def test_take_disabled_joins_the_bid(self):
        places = _up_places(
            _market(up_bid=0.03, up_ask=0.02, dn_bid=0.99),
            take_enabled=False,
        )
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["price"], 0.03)
        self.assertNotEqual(places[0]["tif"], "FAK")

    def test_filled_shares_are_not_bought_again(self):
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.01, up_ask=0.02)],
            intents={"cid": _sold_up()},
            open_orders={},
            now_s=10_000.0 - 100.0,
            enabled=True,
            filled_shares={"cid": {"up": 20}},
        )
        up = [row for row in actions if row["leg"] == "up"]
        self.assertTrue(all(row["op"] == "skip" for row in up))
        self.assertTrue(any(row["reason"] == "filled" for row in up))
        places = _up_places(
            _market(up_bid=0.01, up_ask=0.02),
            filled_shares={"cid": {"up": 8}},
        )
        self.assertEqual(places[0]["shares"], 12.0)
        self.assertEqual(places[0]["style"], "take")

    def test_buy_fill_ignores_usdc_making_amount(self):
        self.assertEqual(
            buy_matched_shares({"size_matched": 20, "makingAmount": 0.40}, 20),
            20.0,
        )
        self.assertEqual(buy_matched_shares({"makingAmount": 400_000}, 20), 0.0)
        self.assertEqual(buy_matched_shares({"takingAmount": "20000000"}, 20), 20.0)
        self.assertEqual(buy_matched_shares({"takingAmount": 8}, 20), 8.0)

    def test_partial_fill_suppresses_miss_and_full_fill_idles(self):
        intent = {
            "status": "confirmed",
            "sold_loser": True,
            "sold_leg": "up",
            "end_ts": 10_000.0,
            "last_sell_attempt_at": 1_000.0,
        }
        events, _flat, _emit = sister_miss_events(
            intents={"cid": intent},
            open_orders={},
            now_s=1_030.0,
            filled_shares={"cid": {"up": 8}},
        )
        self.assertEqual(events, [])
        idle = sister_poll_s(
            intents={"cid": intent},
            open_orders={},
            now_s=9_000.0,
            poll_s=2.0,
            hot_poll_s=1.0,
            enabled=True,
            filled_shares={"cid": {"up": 20}},
            shares=20.0,
        )
        self.assertEqual(idle, 2.0)
        hot = sister_poll_s(
            intents={"cid": intent},
            open_orders={},
            now_s=9_000.0,
            poll_s=2.0,
            hot_poll_s=1.0,
            enabled=True,
            filled_shares={"cid": {"up": 8}},
            shares=20.0,
        )
        self.assertEqual(hot, 1.0)


if __name__ == "__main__":
    unittest.main()
