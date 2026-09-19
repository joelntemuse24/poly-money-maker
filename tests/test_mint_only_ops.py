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
            }
        }
        self.assertEqual(count(state, now=now), 2)


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
            {"__init__.py", "book.py", "chain.py", "contracts.py", "market.py"},
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
