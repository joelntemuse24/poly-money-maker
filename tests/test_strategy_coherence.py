"""Fail-closed hourly strategy coherence (no buybothourly import)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from buy.strategy_coherence import (
    soft_edge_has_price_edge,
    validate_hourly_strategy_coherence,
)

ROOT = Path(__file__).resolve().parents[1]
BOT_HR = ROOT / "buybothourly.py"

# Live VM intent 2026-09-13 ~02:05 UTC (soft-edge max just cut 50 → 7).
LIVE_INTENT_SEP13 = {
    "underlying_gate_enabled": True,
    "min_underlying_edge_usd": 40.0,
    "soft_edge_exit_enabled": True,
    "soft_edge_exit_max_usd": 7.0,
    "soft_edge_exit_bid": 0.95,
    "soft_edge_exit_persist_s": 2.0,
    "a22_window_min": 10.0,
    "a22_min_price": 0.949,
    "b15_window_min": 15.0,
    "buy_threshold": 0.90,
    "buy_max_price": 0.94,
    "c5_window_min": 0.0,
    "c5_min_price": 0.96,
    "hedge_toxic_bid_max": 0.35,
    "hedge_threshold": 0.50,
    "hedge_recovery_cancel": 0.53,
}


def _defaults() -> dict:
    tree = ast.parse(BOT_HR.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_STRATEGY_DEFAULTS":
                return ast.literal_eval(node.value)
    raise AssertionError("_STRATEGY_DEFAULTS not found")


class SoftEdgePriceEdgeTests(unittest.TestCase):
    def test_entry_below_exit_bid_has_price_edge(self):
        self.assertTrue(soft_edge_has_price_edge(0.94, 0.95))

    def test_entry_at_or_above_exit_bid_has_no_price_edge(self):
        self.assertFalse(soft_edge_has_price_edge(0.95, 0.95))
        self.assertFalse(soft_edge_has_price_edge(0.99, 0.95))


class SoftEdgeFloorTests(unittest.TestCase):
    def test_live_intent_after_max_cut_loads(self):
        validate_hourly_strategy_coherence(LIVE_INTENT_SEP13)

    def test_rejects_soft_edge_max_at_or_above_buy_edge_floor(self):
        # Joel: $40 buy floor + $50 soft-edge max made every intentional
        # $40–50 fill an auto dump candidate.
        bad = dict(LIVE_INTENT_SEP13, soft_edge_exit_max_usd=50.0)
        with self.assertRaises(ValueError) as ctx:
            validate_hourly_strategy_coherence(bad)
        self.assertIn("soft_edge_exit_max_usd", str(ctx.exception))
        self.assertIn("min_underlying_edge_usd", str(ctx.exception))

        equal = dict(LIVE_INTENT_SEP13, soft_edge_exit_max_usd=40.0)
        with self.assertRaises(ValueError) as ctx:
            validate_hourly_strategy_coherence(equal)
        self.assertIn("strictly below", str(ctx.exception))

    def test_disabled_soft_edge_skips_floor_rule(self):
        cfg = dict(
            LIVE_INTENT_SEP13,
            soft_edge_exit_enabled=False,
            soft_edge_exit_max_usd=50.0,
        )
        validate_hourly_strategy_coherence(cfg)

    def test_skips_floor_rule_when_underlying_gate_off(self):
        cfg = dict(
            LIVE_INTENT_SEP13,
            underlying_gate_enabled=False,
            soft_edge_exit_max_usd=50.0,
        )
        validate_hourly_strategy_coherence(cfg)


class SoftEdgeBandTests(unittest.TestCase):
    def test_rejects_exit_bid_not_above_a22_floor(self):
        bad = dict(LIVE_INTENT_SEP13, soft_edge_exit_bid=0.949)
        with self.assertRaises(ValueError) as ctx:
            validate_hourly_strategy_coherence(bad)
        self.assertIn("a22_min_price", str(ctx.exception))

    def test_rejects_exit_bid_not_above_b15_cap(self):
        bad = dict(LIVE_INTENT_SEP13, buy_max_price=0.95, a22_window_min=0.0)
        with self.assertRaises(ValueError) as ctx:
            validate_hourly_strategy_coherence(bad)
        self.assertIn("buy_max_price", str(ctx.exception))

    def test_disabled_slice_is_not_checked(self):
        cfg = dict(
            LIVE_INTENT_SEP13,
            a22_window_min=0.0,
            a22_min_price=0.96,
            soft_edge_exit_bid=0.95,
        )
        validate_hourly_strategy_coherence(cfg)


class HedgeLadderTests(unittest.TestCase):
    def test_keeps_dump_lt_qualify_le_recovery(self):
        validate_hourly_strategy_coherence(LIVE_INTENT_SEP13)
        bad = dict(LIVE_INTENT_SEP13, hedge_toxic_bid_max=0.50)
        with self.assertRaises(ValueError) as ctx:
            validate_hourly_strategy_coherence(bad)
        self.assertIn("dump < qualify <= recovery_cancel", str(ctx.exception))

        inverted = dict(LIVE_INTENT_SEP13, hedge_recovery_cancel=0.49)
        with self.assertRaises(ValueError) as ctx:
            validate_hourly_strategy_coherence(inverted)
        self.assertIn("recovery_cancel", str(ctx.exception))


class HourlyDefaultsAndWiringTests(unittest.TestCase):
    def test_defaults_include_soft_edge_keys_disabled(self):
        defaults = _defaults()
        self.assertIn("soft_edge_exit_enabled", defaults)
        self.assertIs(defaults["soft_edge_exit_enabled"], False)
        validate_hourly_strategy_coherence(defaults)

    def test_example_and_snapshot_pass_with_defaults(self):
        defaults = _defaults()
        for name in ("strategy_buyhourly.example.json", "strategy_buyhourly.json"):
            overrides = json.loads((ROOT / name).read_text())
            cfg = dict(defaults)
            cfg.update(overrides)
            validate_hourly_strategy_coherence(cfg)

    def test_load_strategy_calls_coherence_helper(self):
        src = BOT_HR.read_text()
        self.assertIn("from buy.strategy_coherence import", src)
        self.assertIn("validate_hourly_strategy_coherence", src)
        tree = ast.parse(src)
        func = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "load_strategy"
        )
        called = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "validate_hourly_strategy_coherence"
            for n in ast.walk(func)
        )
        self.assertTrue(called, "load_strategy must call validate_hourly_strategy_coherence")


class LoadStrategyFailClosedTests(unittest.TestCase):
    """Exercise extracted load_strategy so the absurd combo cannot load."""

    @classmethod
    def setUpClass(cls):
        src = BOT_HR.read_text()
        tree = ast.parse(src)
        chunks = []
        defaults = None
        load_fn = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_STRATEGY_DEFAULTS":
                        defaults = ast.literal_eval(node.value)
            if isinstance(node, ast.FunctionDef) and node.name == "load_strategy":
                load_fn = ast.get_source_segment(src, node)
        if defaults is None or load_fn is None:
            raise AssertionError("could not extract load_strategy")
        cls.defaults = defaults
        cls.load_src = load_fn

    def _load(self, overrides: dict) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "strategy_buyhourly.json"
        path.write_text(json.dumps(overrides))
        ns = {
            "os": __import__("os"),
            "json": json,
            "math": __import__("math"),
            "STRATEGY_FILE": str(path),
            "_STRATEGY_DEFAULTS": dict(self.defaults),
            "_strat_cache": None,
            "_strat_mtime": 0.0,
            "EXPECTED_TICK_SIZE": "0.01",
            "validate_hourly_strategy_coherence": validate_hourly_strategy_coherence,
            "console": type("C", (), {"print": staticmethod(lambda *_a, **_k: None)})(),
        }
        exec(compile(self.load_src, "buybothourly.py", "exec"), ns, ns)
        return ns["load_strategy"]()

    def test_absurd_soft_edge_floor_cannot_load_at_startup(self):
        good = json.loads((ROOT / "strategy_buyhourly.example.json").read_text())
        payload = dict(good)
        payload.update(
            soft_edge_exit_enabled=True,
            soft_edge_exit_max_usd=50.0,
            min_underlying_edge_usd=40.0,
            underlying_gate_enabled=True,
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._load(payload)
        self.assertIn("valid strategy file", str(ctx.exception))

    def test_live_intent_soft_edge_keys_load_when_otherwise_valid(self):
        good = json.loads((ROOT / "strategy_buyhourly.example.json").read_text())
        payload = dict(good)
        payload.update(
            soft_edge_exit_enabled=True,
            soft_edge_exit_max_usd=7.0,
            min_underlying_edge_usd=40.0,
            underlying_gate_enabled=True,
            soft_edge_exit_bid=0.95,
        )
        cfg = self._load(payload)
        self.assertIs(cfg["soft_edge_exit_enabled"], True)
        self.assertEqual(cfg["soft_edge_exit_max_usd"], 7.0)
        self.assertIs(cfg["entry_enabled"], False)  # example stays disarmed


if __name__ == "__main__":
    unittest.main()
