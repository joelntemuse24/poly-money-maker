"""Mint-only + 15m pathlog operational contracts (no bot imports)."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

import pathlog


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
MINT_EXAMPLE = ROOT / "strategy_mint.example.json"
DEPLOY = ROOT / "deploy"
BUY = ROOT / "buy"


def _assign(name: str):
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in mintbot.py")


def _fn(name: str, extras: dict | None = None):
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    want = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            want = node
            break
    if want is None:
        raise AssertionError(f"{name} not found")
    ns: dict = {"time": __import__("time")}
    if extras:
        ns.update(extras)
    exec(compile(ast.Module(body=[want], type_ignores=[]), str(MINT), "exec"), ns)
    return ns[name]


class PathlogSeriesTests(unittest.TestCase):
    def test_series_is_15m_only(self):
        self.assertEqual(pathlog.SERIES, ["btc-up-or-down-15m"])
        self.assertNotIn("btc-up-or-down-5m", pathlog.SERIES)
        self.assertNotIn("btc-up-or-down-hourly", pathlog.SERIES)
        src = (ROOT / "pathlog.py").read_text()
        self.assertIn(
            "Mint-only stack (2026-09-19): pathlog records 15m only.",
            src,
        )


class MintDefaultsTests(unittest.TestCase):
    def test_example_and_defaults_are_15m_only(self):
        example = json.loads(MINT_EXAMPLE.read_text())
        defaults = _assign("DEFAULTS")
        for blob, label in ((example, "example"), (defaults, "defaults")):
            self.assertEqual(blob["series_slugs"], ["btc-up-or-down-15m"], label)
            self.assertIs(blob["entry_enabled"], False, label)
            self.assertIs(blob["dry_run"], True, label)
            self.assertIs(blob["sell_enabled"], False, label)
        self.assertEqual(defaults["shares"], example["shares"])
        self.assertEqual(defaults["enter_max_ttm_min"], example["enter_max_ttm_min"])
        self.assertEqual(defaults["max_open_sets"], example["max_open_sets"])
        for blob, label in ((example, "example"), (defaults, "defaults")):
            self.assertEqual(blob["sell_threshold"], 0.03, label)
            self.assertEqual(blob["sell_floor"], 0.02, label)
            self.assertAlmostEqual(blob["sell_opposite_min"], 0.90, msg=label)
            self.assertEqual(blob["sell_persist_s"], 5.0, label)
            self.assertAlmostEqual(blob["sell_winner_min"], 0.999, msg=label)
            self.assertEqual(blob["sell_min_bid_size"], 1.0, label)

    def test_open_intent_count_ignores_expired_redeem_holds(self):
        statuses = frozenset(
            {
                "submitting",
                "pending",
                "executed",
                "mined",
                "confirmed_waiting_inventory",
                "confirmed",
            }
        )
        count = _fn("open_intent_count", {"ACTIVE_STATUSES": statuses})
        now = 1_000_000.0
        state = {
            "intents": {
                "live": {"status": "confirmed", "end_ts": now + 60},
                "expired": {"status": "confirmed", "end_ts": now - 121},
                "pending": {"status": "pending", "end_ts": now + 10},
                "done": {"status": "completed", "end_ts": now + 10},
                "winner_only": {
                    "status": "confirmed",
                    "end_ts": now + 60,
                    "sold_loser": True,
                },
                "sold_leg_only": {
                    "status": "confirmed",
                    "end_ts": now + 60,
                    "sold_leg": "dn",
                },
            }
        }
        self.assertEqual(count(state, now=now), 2)


_ACTIVE = frozenset(
    {
        "submitting",
        "pending",
        "executed",
        "mined",
        "confirmed_waiting_inventory",
        "confirmed",
    }
)
_WIN = 900.0
_START_A = 10_000.0  # 1:30
_END_A = _START_A + _WIN  # 1:45
_START_B = _END_A  # 1:45
_END_B = _START_B + _WIN  # 2:00
_START_C = _END_B  # 2:00
_NOW = _END_A - 60.0  # ~1:44, still holding 1:30–1:45


def _slots():
    extras = {"ACTIVE_STATUSES": _ACTIVE}
    return (
        _fn("open_intent_count", extras),
        _fn("mint_slots_full", extras),
    )


class MintSlotChainTests(unittest.TestCase):
    def test_redeem_hold_does_not_block(self):
        count, slots = _slots()
        cfg = {"max_open_sets": 1}
        for flag in (
            {"sold_loser": True},
            {"sold_leg": "dn"},
        ):
            state = {
                "intents": {
                    "a": {
                        "status": "confirmed",
                        "start_ts": _START_A,
                        "end_ts": _END_A,
                        **flag,
                    }
                }
            }
            self.assertEqual(count(state, now=_NOW), 0, flag)
            self.assertFalse(slots(state, cfg, _NOW, _START_B), flag)

    def test_adjacent_next_window_allowed_at_cap(self):
        _, slots = _slots()
        state = {
            "intents": {
                "a": {
                    "status": "confirmed",
                    "start_ts": _START_A,
                    "end_ts": _END_A,
                }
            }
        }
        self.assertFalse(slots(state, {"max_open_sets": 1}, _NOW, _START_B))

    def test_second_lookahead_blocked_if_adjacent_already_held(self):
        _, slots = _slots()
        state = {
            "intents": {
                "a": {
                    "status": "confirmed",
                    "start_ts": _START_A,
                    "end_ts": _END_A,
                },
                "b": {
                    "status": "confirmed",
                    "start_ts": _START_B,
                    "end_ts": _END_B,
                },
            }
        }
        self.assertTrue(slots(state, {"max_open_sets": 1}, _NOW, _START_C))

    def test_non_adjacent_future_window_blocked_at_cap(self):
        _, slots = _slots()
        state = {
            "intents": {
                "a": {
                    "status": "confirmed",
                    "start_ts": _START_A,
                    "end_ts": _END_A,
                }
            }
        }
        self.assertTrue(slots(state, {"max_open_sets": 1}, _NOW, _START_C))

    def test_run_cycle_picks_then_gates_and_records_start_ts(self):
        src = MINT.read_text()
        cycle = src[src.find("def run_cycle") : src.find("\ndef main")]
        self.assertLess(cycle.find("pick = market"), cycle.find("if mint_slots_full"))
        self.assertIn(
            "if mint_slots_full(state, cfg, now, float(pick.start_ts)):",
            cycle,
        )
        self.assertNotIn(
            'if open_intent_count(state) >= int(cfg["max_open_sets"]):',
            cycle,
        )
        self.assertGreaterEqual(cycle.count('"start_ts": pick.start_ts'), 2)

    def test_already_minted_treats_failed_as_attempted(self):
        fn = _fn("already_minted", {"ACTIVE_STATUSES": _ACTIVE})
        cfg = {"one_entry_per_market": True}
        cid = "btc-updown-15m-1789798500"
        state = {"intents": {cid: {"status": "failed", "condition_id": cid}}}
        self.assertTrue(fn(state, cid, cfg))
        self.assertTrue(
            fn(
                {"intents": {cid: {"status": "completed"}}},
                cid,
                cfg,
            )
        )
        self.assertTrue(
            fn(
                {"intents": {cid: {"status": "confirmed"}}},
                cid,
                cfg,
            )
        )
        self.assertFalse(fn({"intents": {}}, cid, cfg))
        self.assertFalse(
            fn(state, cid, {"one_entry_per_market": False}),
        )


class DeployUnitsTests(unittest.TestCase):
    def test_live_units_are_mint_and_pathlog(self):
        live = {p.name for p in DEPLOY.glob("*.service")}
        self.assertEqual(live, {"polymintbot.service", "polypathlog.service"})

    def test_buybot_sources_and_units_are_gone(self):
        for name in (
            "buybot.py",
            "buybot5m.py",
            "buybothourly.py",
            "complementbot.py",
        ):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertFalse((ROOT / "archive").exists())
        self.assertFalse((DEPLOY / "polybuybot.service").exists())
        self.assertFalse((DEPLOY / "polybuybot5m.service").exists())
        self.assertFalse((DEPLOY / "polybuybothourly.service").exists())
        self.assertFalse((DEPLOY / "polycomplement.service").exists())
        self.assertFalse((DEPLOY / "polydangerzone.service").exists())

    def test_buy_helpers_are_mint_and_pathlog_only(self):
        self.assertEqual(
            {p.name for p in BUY.glob("*.py")},
            {
                "__init__.py",
                "book.py",
                "chain.py",
                "contracts.py",
                "market.py",
                "mint_sell.py",
            },
        )
        market_src = (BUY / "market.py").read_text()
        self.assertNotIn("def entry_seconds_left", market_src)
        self.assertNotIn("def market_is_known_for_buy", market_src)
        self.assertNotIn("def discovery_allows_buy_look", market_src)

    def test_manage_sells_is_noop_when_disabled(self):
        manage = _fn("manage_sells")
        state = {"intents": {"x": {"status": "confirmed", "end_ts": 9_999_999}}}
        manage({"sell_enabled": False}, state, object())
        self.assertNotIn("sold_leg", state["intents"]["x"])

    def test_mintbot_sell_uses_share_leg_sized_bids_and_latch(self):
        src = MINT.read_text()
        self.assertIn("parse_sell_fill_shares", src)
        self.assertIn("inventory_latch", src)
        self.assertIn("best_bid_with_min_size", src)
        self.assertIn("persist_ready", src)
        self.assertIn("loser_persist_ready", src)
        self.assertIn("winner_cashout_leg", src)
        self.assertIn("winner_cheap_decision", src)
        self.assertIn("sell_winner_cheap_denied", src)
        self.assertIn("sell_winner_cheap_allowed", src)
        self.assertIn("last_status=intent.get(\"sell_last_status\")", src)
        mint_sell_src = (BUY / "mint_sell.py").read_text()
        self.assertIn("def empty_fak_status", mint_sell_src)
        self.assertIn("def loser_persist_ready", mint_sell_src)
        self.assertNotIn(
            'for key in ("takingAmount", "makingAmount"',
            src,
        )
        self.assertNotIn("if bal + 1e-9 < tol:", src)

    def test_docs_do_not_start_hourly_dense_or_dangerzone(self):
        for path in (
            ROOT / "AGENTS.md",
            ROOT / "CURRENT.md",
            ROOT / "deploy" / "DISK_OPS.md",
        ):
            text = path.read_text()
            self.assertNotIn("systemctl start pathlog_hourly_dense", text, path.name)
            self.assertNotIn("systemctl enable polydangerzone", text, path.name)
            self.assertNotIn("systemctl start polybuybot ", text, path.name)


if __name__ == "__main__":
    unittest.main()
