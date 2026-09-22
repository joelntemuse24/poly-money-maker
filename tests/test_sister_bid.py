"""Sister scrap-bidder policy. No CLOB, no mintbot import."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

from buy.sister_bid import (
    COMPLEMENT_DEPOSIT,
    MINTBOT_FUNDER,
    SISTER_DEFAULTS,
    a_token_flat,
    plan_sister_bids,
    resolve_sister_client_config,
    sister_cancel_due,
    sister_funder_ok,
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

    def test_rest_still_live_blocks_even_after_sold_flag(self):
        intent = {
            "status": "confirmed",
            "sold_loser": True,
            "sold_leg": "up",
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
        self.assertNotIn("OrderType.FAK", src)
        self.assertNotIn("build_atomic_mint", src)
        self.assertNotIn("submit_mint", src)


if __name__ == "__main__":
    unittest.main()
