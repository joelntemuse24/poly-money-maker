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
    markets_needing_books,
    dump_hedge_leg,
    plan_dump_hedges,
    plan_sister_bids,
    sister_book_wanted,
    sister_dump_on,
    sister_quote,
    sister_scrap_on,
    resolve_sister_client_config,
    sister_cancel_due,
    sister_funder_ok,
    sister_leg_filled,
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
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "rest_cap")
        self.assertEqual(places[0]["tif"], "GTC")
        self.assertEqual(places[0]["tif_why"], "gtc_short_expiry")
        self.assertEqual(places[0]["reason"], "a_flat")
        self.assertIn(places[0]["tif"], ("GTD", "GTC"))
        held = [row for row in actions if row["leg"] == "dn"]
        self.assertTrue(any(row["reason"] == "a_holds_other" for row in held))

    def test_absent_market_does_not_bid_by_default(self):
        now = 10_000.0 - 90.0
        cheap = _market(up_bid=0.02, up_ask=0.03, dn_bid=0.96)
        actions = plan_sister_bids(
            markets=[cheap],
            intents={},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        self.assertFalse(any(row["op"] == "place" for row in actions))
        self.assertTrue(all(row["reason"] == "a_absent" for row in actions))
        resting = plan_sister_bids(
            markets=[cheap],
            intents={},
            open_orders={"cid": {"up": {"order_id": "dust-1"}}},
            now_s=now,
            enabled=True,
        )
        cancels = [row for row in resting if row["op"] == "cancel"]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["reason"], "a_absent")
        self.assertEqual(cancels[0]["order_id"], "dust-1")
        opted = plan_sister_bids(
            markets=[cheap],
            intents={},
            open_orders={},
            now_s=now,
            enabled=True,
            absent_enabled=True,
        )
        places = [row for row in opted if row["op"] == "place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["leg"], "up")
        self.assertEqual(places[0]["reason"], "a_absent")

    def test_flat_after_scrap_still_takes(self):
        now = 10_000.0 - 90.0
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.02, up_ask=0.03, dn_bid=0.96)],
            intents={"cid": _sold_up()},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["leg"], "up")
        self.assertEqual(places[0]["reason"], "a_flat")
        self.assertEqual(places[0]["style"], "take")
        self.assertEqual(places[0]["tif"], "FAK")
        self.assertFalse(any(row["reason"] == "a_absent" and row["op"] == "place" for row in actions))

    def test_absent_market_skips_when_neither_side_is_cheap(self):
        now = 10_000.0 - 90.0
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.40, dn_bid=0.60)],
            intents={},
            open_orders={},
            now_s=now,
            enabled=True,
            absent_enabled=True,
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
        self.assertTrue(all(row["reason"] == "a_absent" for row in actions))
        self.assertFalse(any(row["op"] == "place" for row in actions))
        opted = plan_sister_bids(
            markets=[_market()],
            intents={},
            open_orders={},
            now_s=now,
            enabled=True,
            absent_enabled=True,
        )
        self.assertTrue(all(row["reason"] == "too_early" for row in opted))

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
        self.assertTrue(all(row["reason"] == "a_absent" for row in absent))
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
    def test_defaults_are_20_shares_at_five_cents(self):
        self.assertEqual(SISTER_DEFAULTS["shares"], 20.0)
        self.assertEqual(SISTER_DEFAULTS["bid_max_px"], 0.05)
        self.assertEqual(SISTER_DEFAULTS["bid_rest_px"], 0.05)
        self.assertEqual(SISTER_DEFAULTS["bid_fak_min_notional"], 1.0)
        self.assertEqual(SISTER_DEFAULTS["bid_fak_max_notional"], 1.5)
        self.assertEqual(SISTER_DEFAULTS["min_gtd_ahead_s"], 180.0)
        self.assertIs(SISTER_DEFAULTS["bid_enabled"], False)
        self.assertIs(SISTER_DEFAULTS["dry_run"], True)
        self.assertEqual(SISTER_DEFAULTS["active_ttm_s"], 180.0)
        self.assertEqual(SISTER_DEFAULTS["cancel_ttm_s"], 20.0)
        self.assertEqual(SISTER_DEFAULTS["poll_hot_s"], 1.0)
        self.assertEqual(SISTER_DEFAULTS["miss_after_s"], 10.0)
        self.assertEqual(SISTER_DEFAULTS["miss_throttle_s"], 30.0)
        self.assertIs(SISTER_DEFAULTS["bid_take_enabled"], True)
        self.assertIs(SISTER_DEFAULTS["bid_absent_enabled"], False)
        self.assertIs(SISTER_DEFAULTS["scrap_hedge_enabled"], False)
        self.assertIs(SISTER_DEFAULTS["dump_hedge_enabled"], True)
        self.assertEqual(SISTER_DEFAULTS["dump_hedge_shares"], 10.0)
        self.assertIs(SISTER_DEFAULTS["topup_enabled"], False)
        self.assertEqual(SISTER_DEFAULTS["dump_hedge_rest_px"], 0.10)
        self.assertEqual(SISTER_DEFAULTS["dump_hedge_fak_min_notional"], 1.0)
        self.assertEqual(SISTER_DEFAULTS["dump_hedge_fak_max_notional"], 1.5)
        self.assertEqual(SISTER_DEFAULTS["topup_usd"], 5.0)
        self.assertEqual(SISTER_DEFAULTS["topup_need_usd"], 1.5)
        example = json.loads(
            (ROOT / "strategy_scrapbid.example.json").read_text(encoding="utf-8")
        )
        self.assertIs(example["bid_take_enabled"], True)
        self.assertIs(example["bid_absent_enabled"], False)
        self.assertEqual(example["bid_max_px"], 0.05)
        self.assertEqual(example["bid_rest_px"], 0.05)
        self.assertEqual(example["bid_fak_min_notional"], 1.0)
        self.assertEqual(example["bid_fak_max_notional"], 1.5)
        self.assertEqual(example["min_gtd_ahead_s"], 180.0)
        self.assertEqual(example["shares"], 20.0)
        self.assertEqual(example["dump_hedge_shares"], 10.0)
        self.assertIs(example["scrap_hedge_enabled"], False)
        self.assertIs(example["dump_hedge_enabled"], True)
        self.assertIs(example["topup_enabled"], False)
        self.assertIs(example["bid_absent_enabled"], False)
        self.assertEqual(example["topup_usd"], 5.0)
        self.assertEqual(example["topup_need_usd"], 1.5)
        self.assertEqual(sister_leg_filled({"cid": {"up": 3}}, "cid", "up"), 3.0)
        self.assertEqual(sister_leg_filled({}, "cid", "up"), 0.0)

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
        self.assertIn("bid_absent_enabled", src)
        self.assertIn("plan_dump_hedges", src)
        self.assertIn("sister_topup.py", src)
        self.assertIn("bid_rest_px", src)
        self.assertIn("tif_why", src)
        self.assertNotIn("no_fill_dwell", src)
        self.assertNotIn("bid_escalate", src)
        policy = (ROOT / "buy" / "sister_bid.py").read_text(encoding="utf-8")
        self.assertIn("gtc_short_expiry", policy)
        self.assertIn("rest_cap", policy)
        self.assertIn("fak_floor", policy)
        self.assertIn("live_ask", policy)
        self.assertIn("cross_ask", policy)
        self.assertIn("bid_fak_max_notional", src)
        self.assertIn("_fetch_top", src)
        self.assertIn("sister_book_wanted", src)
        self.assertIn("_mint_intents", src)
        self.assertNotIn("build_atomic_mint", src)
        calls = _call_names(_function(tree, "run_once"))
        first_read = calls.index("_mint_intents")
        first_books = calls.index("_fill_books")
        second_read = calls.index("_mint_intents", first_read + 1)
        second_books = calls.index("_fill_books", first_books + 1)
        self.assertLess(first_read, first_books)
        self.assertLess(first_books, second_read)
        self.assertLess(second_read, second_books)
        self.assertLess(second_books, calls.index("plan_sister_bids"))
        self.assertLess(calls.index("plan_sister_bids"), calls.index("plan_dump_hedges"))
        self.assertNotIn("submit_mint", src)
        self.assertIn("scrapbid_miss", src)
        self.assertIn("sister_scrap_on", src)
        self.assertIn("sister_dump_on", src)
        self.assertIn('cfg.get("topup_enabled", False)', src)
        topup_src = (ROOT / "sister_topup.py").read_text(encoding="utf-8")
        self.assertIn('cfg.get("topup_enabled", False)', topup_src)
        self.assertIn("sister_poll_s", src)
        self.assertIn("poll_hot_s", src)


class SisterHedgeSwitchTests(unittest.TestCase):
    def test_scrap_stays_off_when_bids_are_on(self):
        cfg = dict(SISTER_DEFAULTS)
        cfg["bid_enabled"] = True
        self.assertFalse(sister_scrap_on(cfg))
        self.assertTrue(sister_dump_on(cfg))

    def test_scrap_on_only_when_both_flags_allow(self):
        cfg = dict(SISTER_DEFAULTS)
        cfg["bid_enabled"] = True
        cfg["scrap_hedge_enabled"] = True
        self.assertTrue(sister_scrap_on(cfg))
        cfg["dump_hedge_enabled"] = False
        self.assertFalse(sister_dump_on(cfg))

    def test_master_bid_switch_blocks_both(self):
        cfg = dict(SISTER_DEFAULTS)
        cfg["scrap_hedge_enabled"] = True
        cfg["dump_hedge_enabled"] = True
        self.assertFalse(sister_scrap_on(cfg))
        self.assertFalse(sister_dump_on(cfg))


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


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _call_names(node: ast.AST) -> list[str]:
    names: list[str] = []

    def walk(current: ast.AST) -> None:
        if isinstance(current, ast.Call):
            func = current.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
        for child in ast.iter_child_nodes(current):
            walk(child)

    walk(node)
    return names


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
    def test_twenty_shares_chase_the_ask_inside_the_budget(self):
        # 20 × 2¢ = $0.40, so the limit lifts to 5¢ ($1) and the fill is the ask.
        places = _up_places(_market(up_bid=0.01, up_ask=0.02, dn_bid=0.99))
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["style"], "take")
        self.assertEqual(places[0]["tif"], "FAK")
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["shares"], 20.0)
        self.assertEqual(places[0]["price_why"], "fak_floor")
        self.assertEqual(places[0]["reason"], "a_flat")
        # 6¢ ask is inside $1.50. Take the ask, not a flat 5¢.
        places = _up_places(_market(up_bid=0.04, up_ask=0.06, dn_bid=0.99))
        self.assertEqual(places[0]["style"], "take")
        self.assertEqual(places[0]["tif"], "FAK")
        self.assertEqual(places[0]["price"], 0.06)
        self.assertEqual(places[0]["price_why"], "live_ask")
        # 7.5¢ is the budget ceiling: 20 × 0.075 = $1.50.
        places = _up_places(_market(up_bid=0.05, up_ask=0.075, dn_bid=0.99))
        self.assertEqual(places[0]["price"], 0.075)
        self.assertEqual(places[0]["price_why"], "live_ask")
        self.assertEqual(places[0]["tif"], "FAK")
        # 100 × 5¢ = $5, above the $1.50 max, and 5¢ would cross a 2¢ ask.
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.01, up_ask=0.02, dn_bid=0.99)],
            intents={"cid": _sold_up()},
            open_orders={},
            now_s=10_000.0 - 100.0,
            enabled=True,
            shares=100,
        )
        up = [row for row in actions if row["leg"] == "up"]
        self.assertTrue(any(row["reason"] == "cross_ask" for row in up))
        self.assertFalse(any(row["op"] == "place" for row in up))

    def test_notional_above_max_rests_only_when_it_does_not_cross(self):
        places = _up_places(
            _market(up_bid=0.02, up_ask=0.08, dn_bid=0.99),
            shares=40,
        )
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "rest_cap")
        self.assertNotEqual(places[0]["tif"], "FAK")

    def test_cheap_bid_still_rests_at_the_cap(self):
        places = _up_places(_market(up_bid=0.03, up_ask=None, dn_bid=0.99))
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "rest_cap")
        self.assertNotEqual(places[0]["tif"], "FAK")
        places = _up_places(_market(up_bid=0.03, up_ask=0.08, dn_bid=0.99))
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "rest_cap")

    def test_empty_book_rests_at_five_cents(self):
        places = _up_places(_market(up_bid=None, up_ask=None, dn_bid=0.99))
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "rest_cap")
        self.assertNotEqual(places[0]["tif"], "FAK")

    def test_never_pays_above_the_dollar_fifty_budget(self):
        places = _up_places(_market(up_bid=0.04, up_ask=0.08, dn_bid=0.99))
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "rest_cap")
        self.assertEqual(places[0]["style"], "rest")
        self.assertLessEqual(places[0]["price"], 0.075)
        places = _up_places(_market(up_bid=0.04, up_ask=0.05, dn_bid=0.99))
        self.assertEqual(places[0]["style"], "take")
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "live_ask")
        self.assertEqual(places[0]["tif"], "FAK")
        style, price, why = sister_quote(
            bid=0.03,
            ask=0.02,
            bid_max_px=0.05,
            shares=20,
            take_enabled=False,
        )
        self.assertEqual((style, price, why), ("wait", 0.0, "cross_ask"))

    def test_rich_ask_rests_at_cap_instead_of_taking(self):
        places = _up_places(_market(up_bid=None, up_ask=0.08, dn_bid=0.99))
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["style"], "rest")
        self.assertNotEqual(places[0]["tif"], "FAK")
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "rest_cap")

    def test_crossed_rest_is_not_posted_when_fak_is_blocked(self):
        places = _up_places(
            _market(up_bid=0.03, up_ask=0.02, dn_bid=0.99),
            take_enabled=False,
        )
        self.assertEqual(places, [])
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.03, up_ask=0.02, dn_bid=0.99)],
            intents={"cid": _sold_up()},
            open_orders={},
            now_s=10_000.0 - 100.0,
            enabled=True,
            take_enabled=False,
        )
        up = [row for row in actions if row["leg"] == "up"]
        self.assertTrue(any(row["reason"] == "cross_ask" for row in up))
        # Ask above the cap: the 5¢ rest does not cross.
        places = _up_places(
            _market(up_bid=0.03, up_ask=0.08, dn_bid=0.99),
            take_enabled=False,
        )
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["price"], 0.05)
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
        self.assertEqual(places, [])
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.01, up_ask=None)],
            intents={"cid": _sold_up()},
            open_orders={},
            now_s=10_000.0 - 100.0,
            enabled=True,
            filled_shares={"cid": {"up": 8}},
        )
        places = [row for row in actions if row["op"] == "place" and row["leg"] == "up"]
        self.assertEqual(places[0]["shares"], 12.0)
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["price"], 0.05)

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


class BookFilterTests(unittest.TestCase):
    def test_far_window_is_not_quoted_until_sold(self):
        # btc-updown-15m-1790106300 ended 20:00 UTC; at the scrap TTM was 316s.
        end = 17_901_07200.0
        now = end - 316.0
        cid = "0x02ce55357529aedc3c7407bf8c30a6e9044679695bcf803a9b724f8e6183aa92"
        live = _market(condition_id=cid, end_ts=end)
        future = _market(
            condition_id="future",
            end_ts=now + 3600.0,
            up_token="FUT_UP",
            dn_token="FUT_DN",
        )
        held = {"status": "confirmed", "sold_loser": False, "sell_loser_leg": "up"}
        before = markets_needing_books(
            [live, future],
            {cid: held},
            {},
            now_s=now,
        )
        self.assertEqual(before, [])
        self.assertFalse(
            sister_book_wanted(live, held, {}, now_s=now)
        )
        sold = {"status": "confirmed", "sold_loser": True, "sold_leg": "up"}
        after = markets_needing_books(
            [live, future],
            {cid: sold},
            {},
            now_s=now,
        )
        self.assertEqual([row["condition_id"] for row in after], [cid])

    def test_late_window_and_open_order_are_quoted(self):
        now = 10_000.0
        near = _market(condition_id="near", end_ts=now + 100.0)
        future = _market(condition_id="future", end_ts=now + 3600.0)
        chosen = markets_needing_books([near, future], {}, {}, now_s=now)
        self.assertEqual([row["condition_id"] for row in chosen], ["near"])
        resting = markets_needing_books(
            [future],
            {},
            {"future": {"up": {"order_id": "bid-1"}}},
            now_s=now,
        )
        self.assertEqual([row["condition_id"] for row in resting], ["future"])
        self.assertFalse(
            sister_book_wanted(
                _market(end_ts=now + 10.0),
                {"status": "confirmed", "sold_loser": True, "sold_leg": "up"},
                {},
                now_s=now,
            )
        )

    def test_completed_sold_loser_still_posts_scrap(self):
        intent = {"status": "completed", "sold_loser": True, "sold_leg": "up"}
        flat, why = a_token_flat(intent, "up")
        self.assertTrue(flat)
        self.assertEqual(why, "a_flat")
        other, other_why = a_token_flat(intent, "dn")
        self.assertFalse(other)
        self.assertEqual(other_why, "a_holds_other")
        absent, absent_why = a_token_flat({"status": "completed"}, "up")
        self.assertTrue(absent)
        self.assertEqual(absent_why, "a_absent")
        now = 10_000.0 - 316.0
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.01, up_ask=0.02, dn_bid=0.99)],
            intents={"cid": intent},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["leg"], "up")
        self.assertEqual(places[0]["reason"], "a_flat")
        self.assertEqual(places[0]["style"], "take")
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "fak_floor")
        self.assertEqual(places[0]["tif"], "FAK")
        self.assertGreater(places[0]["ttm_s"], 180.0)
        held = [row for row in actions if row["leg"] == "dn"]
        self.assertTrue(any(row["reason"] == "a_holds_other" for row in held))


class RestCapAndGtcTests(unittest.TestCase):
    def test_incident_window_posts_gtc_at_five_cents(self):
        # btc-updown-15m-1790109900, about 186s left: GTD expiry is ~166s.
        # No ask, so the FAK does not fire and the rest is GTC at 5¢.
        end = 17_901_09900.0 + 900.0
        now = end - 186.0
        actions = plan_sister_bids(
            markets=[_market(end_ts=end, up_bid=0.02, up_ask=None, dn_bid=0.97)],
            intents={"cid": _sold_up()},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["leg"], "up")
        self.assertEqual(places[0]["price"], 0.05)
        self.assertEqual(places[0]["price_why"], "rest_cap")
        self.assertEqual(places[0]["tif"], "GTC")
        self.assertEqual(places[0]["expiration"], 0)
        self.assertEqual(places[0]["tif_why"], "gtc_short_expiry")
        self.assertNotEqual(places[0]["tif"], "FAK")

    def test_long_window_still_uses_gtd(self):
        end = 10_000.0
        now = end - 400.0
        actions = plan_sister_bids(
            markets=[_market(end_ts=end, up_bid=None, up_ask=None)],
            intents={"cid": _sold_up()},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place" and row["leg"] == "up"]
        self.assertEqual(places[0]["tif"], "GTD")
        self.assertEqual(places[0]["tif_why"], "gtd")
        self.assertEqual(places[0]["price"], 0.05)
        self.assertGreaterEqual(places[0]["expiration"], int(now) + 180)

    def test_resting_order_is_kept_not_escalated(self):
        now = 10_000.0 - 200.0
        actions = plan_sister_bids(
            markets=[_market(up_bid=0.02, up_ask=0.03)],
            intents={"cid": _sold_up()},
            open_orders={
                "cid": {
                    "up": {
                        "order_id": "bid-1",
                        "price": 0.03,
                        "placed_at": now - 60.0,
                    }
                }
            },
            now_s=now,
            enabled=True,
        )
        up = [row for row in actions if row["leg"] == "up"]
        self.assertEqual([row["op"] for row in up], ["keep"])
        self.assertFalse(any(row["op"] == "place" for row in actions))
        src = (ROOT / "buy" / "sister_bid.py").read_text(encoding="utf-8")
        self.assertNotIn("no_fill_dwell", src)
        self.assertNotIn("bid_escalate", src)


def _dumped_up():
    """A scrapped DN, then normal-dumped the held UP leg."""
    return {
        "status": "confirmed",
        "sold_loser": True,
        "sold_leg": "dn",
        "sold_dump": True,
        "sell_dump_leg": "up",
        "end_ts": 10_000.0,
    }


class DumpHedgeTests(unittest.TestCase):
    def test_dump_up_buys_ten_dn(self):
        now = 10_000.0 - 100.0
        self.assertEqual(dump_hedge_leg(_dumped_up()), "dn")
        actions = plan_dump_hedges(
            markets=[_market(dn_bid=0.04, dn_ask=0.08, up_bid=0.70, up_ask=0.72)],
            intents={"cid": _dumped_up()},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["leg"], "dn")
        self.assertEqual(places[0]["shares"], 10.0)
        self.assertEqual(places[0]["book"], "dump")
        self.assertEqual(places[0]["reason"], "a_dump")
        self.assertEqual(places[0]["style"], "take")
        self.assertEqual(places[0]["price"], 0.10)
        self.assertEqual(places[0]["price_why"], "fak_floor")
        self.assertEqual(places[0]["tif"], "FAK")

    def test_dump_dn_buys_ten_up(self):
        now = 10_000.0 - 100.0
        intent = {
            "status": "confirmed",
            "sold_loser": True,
            "sold_leg": "up",
            "sold_dump": True,
            "sell_dump_leg": "dn",
            "end_ts": 10_000.0,
        }
        actions = plan_dump_hedges(
            markets=[_market(up_bid=0.20, up_ask=0.12, dn_bid=0.70, dn_ask=0.72)],
            intents={"cid": intent},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual([row["leg"] for row in places], ["up"])
        self.assertEqual(places[0]["shares"], 10.0)
        self.assertEqual(places[0]["price"], 0.12)
        self.assertEqual(places[0]["price_why"], "live_ask")

    def test_rich_other_side_rests_under_the_ask(self):
        now = 10_000.0 - 100.0
        actions = plan_dump_hedges(
            markets=[_market(dn_bid=0.70, dn_ask=0.72)],
            intents={"cid": _dumped_up()},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        places = [row for row in actions if row["op"] == "place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["style"], "rest")
        self.assertEqual(places[0]["price"], 0.10)
        self.assertEqual(places[0]["shares"], 10.0)

    def test_winner_cashout_and_flat_inventory_do_not_hedge(self):
        now = 10_000.0 - 100.0
        winner = {"status": "confirmed", "sold_winner": True, "sold_leg": "up", "end_ts": 10_000.0}
        flat = {"status": "confirmed", "sold_dump": True, "sold_leg": "dn", "end_ts": 10_000.0}
        for intent in (winner, flat, None):
            actions = plan_dump_hedges(
                markets=[_market(dn_ask=0.08)],
                intents={"cid": intent} if intent else {},
                open_orders={},
                now_s=now,
                enabled=True,
            )
            self.assertFalse(any(row["op"] == "place" for row in actions))

    def test_absent_markets_stay_unbid(self):
        now = 10_000.0 - 100.0
        actions = plan_dump_hedges(
            markets=[_market(dn_ask=0.04)],
            intents={},
            open_orders={},
            now_s=now,
            enabled=True,
        )
        self.assertFalse(any(row["op"] == "place" for row in actions))

    def test_scrap_twenty_and_dump_ten_are_separate(self):
        now = 10_000.0 - 100.0
        market = _market(dn_bid=0.04, dn_ask=0.06, up_bid=0.70, up_ask=0.72)
        intent = _dumped_up()
        scrap = [
            row for row in plan_sister_bids(
                markets=[market], intents={"cid": intent}, open_orders={},
                now_s=now, enabled=True,
            )
            if row["op"] == "place"
        ]
        dump = [
            row for row in plan_dump_hedges(
                markets=[market], intents={"cid": intent}, open_orders={},
                now_s=now, enabled=True,
            )
            if row["op"] == "place"
        ]
        self.assertEqual([(row["leg"], row["shares"]) for row in scrap], [("dn", 20.0)])
        self.assertEqual([(row["leg"], row["shares"]) for row in dump], [("dn", 10.0)])
        self.assertNotEqual(scrap[0].get("book"), "dump")
        self.assertEqual(dump[0]["book"], "dump")

    def test_filled_dump_clip_does_not_buy_again(self):
        now = 10_000.0 - 100.0
        actions = plan_dump_hedges(
            markets=[_market(dn_ask=0.08)],
            intents={"cid": _dumped_up()},
            open_orders={},
            now_s=now,
            enabled=True,
            filled_shares={"cid": {"dn": 10}},
        )
        self.assertTrue(any(row["reason"] == "filled" for row in actions))
        self.assertFalse(any(row["op"] == "place" for row in actions))

    def test_disabled_places_nothing(self):
        now = 10_000.0 - 100.0
        actions = plan_dump_hedges(
            markets=[_market(dn_ask=0.08)],
            intents={"cid": _dumped_up()},
            open_orders={},
            now_s=now,
            enabled=False,
        )
        self.assertFalse(any(row["op"] == "place" for row in actions))
        self.assertTrue(any(row["reason"] == "disabled" for row in actions))


if __name__ == "__main__":
    unittest.main()
