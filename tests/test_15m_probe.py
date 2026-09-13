"""15m $5 dry-run probe knobs (no buybot.py import)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from buy.probe_15m import (
    ask_in_band,
    in_buy_window,
    live_posting_armed,
    probe_live_flip_note,
    probe_spend_usd,
    shares_rail_needed,
    should_evaluate_entries,
)
from buy.strategy_coherence import validate_15m_strategy_coherence

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "buybot.py"
PROBE = ROOT / "strategy_buy15m_probe.example.json"
HOURLY = ROOT / "buybothourly.py"
HOURLY_JSON = ROOT / "strategy_buyhourly.json"
HOURLY_EXAMPLE = ROOT / "strategy_buyhourly.example.json"


def _defaults() -> dict:
    tree = ast.parse(BOT.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_STRATEGY_DEFAULTS":
                    return ast.literal_eval(node.value)
    raise AssertionError("_STRATEGY_DEFAULTS not found")


class ProbeMathTests(unittest.TestCase):
    def test_window_is_last_180s(self):
        self.assertTrue(in_buy_window(180.0, 3.0))
        self.assertTrue(in_buy_window(1.0, 3.0))
        self.assertFalse(in_buy_window(181.0, 3.0))
        self.assertFalse(in_buy_window(0.0, 3.0))

    def test_ge95_band(self):
        self.assertTrue(ask_in_band(0.95, 0.95, 0.99))
        self.assertTrue(ask_in_band(0.99, 0.95, 0.99))
        self.assertFalse(ask_in_band(0.94, 0.95, 0.99))
        self.assertFalse(ask_in_band(1.00, 0.95, 0.99))
        self.assertFalse(ask_in_band(None, 0.95, 0.99))

    def test_spend_cap_is_json_only(self):
        self.assertEqual(probe_spend_usd(5.0, 5.0, 5.0), 5.0)
        self.assertEqual(probe_spend_usd(2.0, 20.0, 20.0), 2.0)
        self.assertEqual(probe_spend_usd(20.0, 20.0, 20.0), 20.0)
        self.assertEqual(probe_spend_usd(10.0, 11.0, 0.0), 10.0)
        self.assertGreaterEqual(shares_rail_needed(5.0, 0.95), 5.0 / 0.95)

    def test_live_posting_needs_both_knobs(self):
        self.assertFalse(live_posting_armed(True, False))
        self.assertFalse(live_posting_armed(True, True))
        self.assertFalse(live_posting_armed(False, False))
        self.assertTrue(live_posting_armed(False, True))
        self.assertTrue(should_evaluate_entries(True, False))
        self.assertFalse(should_evaluate_entries(False, False))

    def test_live_flip_note_names_the_one_knob(self):
        restart, arm = probe_live_flip_note()
        self.assertIn("dry_run", restart)
        self.assertIn("entry_enabled", arm)


class ProbeJsonTests(unittest.TestCase):
    def test_probe_is_disarmed_five_dollar_ge95(self):
        data = json.loads(PROBE.read_text())
        self.assertIs(data["dry_run"], True)
        self.assertIs(data["entry_enabled"], False)
        self.assertEqual(data["buy_threshold"], 0.95)
        self.assertEqual(data["buy_max_price"], 0.99)
        self.assertEqual(data["buy_window_min"], 3.0)
        self.assertEqual(data["min_underlying_edge_usd"], 10.0)
        self.assertEqual(data["buy_budget"], 5.0)
        self.assertEqual(data["buy_max_spend"], 5.0)
        self.assertEqual(data["market_spend_cap"], 5.0)
        self.assertNotIn("max_open_notional", data)
        self.assertNotIn("max_daily_notional", data)
        self.assertEqual(data["entry_book_persist_s"], 2.0)
        self.assertEqual(data["hedge_persist_s"], 1.0)
        self.assertEqual(data["hedge_dump_persist_s"], 2.0)
        self.assertEqual(data["hedge_threshold"], 0.35)
        self.assertEqual(data["hedge_require_ask_max"], 0.40)
        self.assertIs(data["hedge_require_oracle"], True)
        self.assertIs(data["soft_edge_exit_enabled"], False)
        self.assertIs(data["early_hot_defer_enabled"], False)
        self.assertEqual(data["take_profit_fraction"], 0.0)
        self.assertEqual(data["take_profit_full_bid"], 0.99)
        self.assertNotIn("a22_window_min", data)
        self.assertNotIn("b15_window_min", data)
        validate_15m_strategy_coherence(data)
        self.assertFalse(live_posting_armed(data["dry_run"], data["entry_enabled"]))


class Coherence15mTests(unittest.TestCase):
    def test_rejects_soft_edge_max_at_or_above_floor(self):
        bad = {
            "soft_edge_exit_enabled": True,
            "underlying_gate_enabled": True,
            "soft_edge_exit_max_usd": 12.0,
            "min_underlying_edge_usd": 10.0,
            "soft_edge_exit_bid": 0.95,
            "soft_edge_exit_persist_s": 2.0,
            "buy_window_min": 3.0,
            "buy_budget": 5.0,
            "market_spend_cap": 5.0,
            "early_hot_defer_enabled": False,
        }
        with self.assertRaises(ValueError) as ctx:
            validate_15m_strategy_coherence(bad)
        self.assertIn("soft_edge_exit_max_usd", str(ctx.exception))

    def test_rejects_early_hot(self):
        with self.assertRaises(ValueError) as ctx:
            validate_15m_strategy_coherence({
                "early_hot_defer_enabled": True,
                "buy_window_min": 3.0,
                "buy_budget": 5.0,
            })
        self.assertIn("early_hot", str(ctx.exception))

    def test_rejects_spend_cap_below_budget(self):
        with self.assertRaises(ValueError) as ctx:
            validate_15m_strategy_coherence({
                "buy_window_min": 3.0,
                "buy_budget": 5.0,
                "market_spend_cap": 2.0,
            })
        self.assertIn("market_spend_cap", str(ctx.exception))

    def test_disabled_soft_edge_loads(self):
        validate_15m_strategy_coherence({
            "soft_edge_exit_enabled": False,
            "soft_edge_exit_max_usd": 50.0,
            "underlying_gate_enabled": True,
            "min_underlying_edge_usd": 10.0,
            "buy_window_min": 3.0,
            "buy_budget": 5.0,
            "market_spend_cap": 5.0,
            "early_hot_defer_enabled": False,
        })


class BuybotWiringTests(unittest.TestCase):
    def test_defaults_match_probe_rails(self):
        defaults = _defaults()
        self.assertEqual(defaults["buy_threshold"], 0.95)
        self.assertEqual(defaults["buy_max_price"], 0.99)
        self.assertEqual(defaults["min_underlying_edge_usd"], 10.0)
        self.assertEqual(defaults["buy_budget"], 5.0)
        self.assertEqual(defaults["market_spend_cap"], 5.0)
        self.assertNotIn("max_open_notional", defaults)
        self.assertNotIn("max_daily_notional", defaults)
        self.assertEqual(defaults["entry_book_persist_s"], 2.0)
        self.assertEqual(defaults["hedge_persist_s"], 1.0)
        self.assertEqual(defaults["hedge_dump_persist_s"], 2.0)
        self.assertIs(defaults["hedge_require_oracle"], True)
        self.assertIs(defaults["dry_run"], True)
        self.assertIs(defaults["entry_enabled"], False)
        self.assertIs(defaults["early_hot_defer_enabled"], False)
        self.assertIs(defaults["soft_edge_exit_enabled"], False)
        validate_15m_strategy_coherence(defaults)

    def test_bot_wires_coherence_and_dry_run_eval(self):
        src = BOT.read_text()
        self.assertIn("validate_15m_strategy_coherence", src)
        self.assertIn("should_evaluate_entries(DRY_RUN, ENTRY_ENABLED)", src)
        self.assertIn("probe_spend_usd", src)
        self.assertIn("emit_buy_depth_ladder", src)
        self.assertIn("entry_book_persist_ready", src)
        self.assertIn("hold_while_oracle_agrees", src)
        self.assertIn("hedge_persist_ready", src)
        self.assertIn("take_profit_full_ready", src)
        self.assertIn("POLY_BUY15M_STRATEGY", src)
        self.assertNotIn("hedge_ladder_for_ttm", src)
        self.assertNotIn("hedge_late_ttm_s", src)
        self.assertNotIn("SOURCE_TWAP_30", src)
        self.assertNotIn("SOURCE_TWAP_60", src)
        self.assertNotIn("max_open_notional", src)
        self.assertNotIn("max_daily_notional", src)
        self.assertNotIn("buy_skip_max_notional", src)
        self.assertNotIn("buy_skip_max_daily_notional", src)
        self.assertNotIn("MAX_OPEN_NOTIONAL", src)
        self.assertNotIn("MAX_DAILY_NOTIONAL", src)

    def test_load_strategy_rejects_soft_edge_above_floor(self):
        src = BOT.read_text()
        tree = ast.parse(src)
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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "strategy_buy.json"
        payload = json.loads(PROBE.read_text())
        payload.update(
            soft_edge_exit_enabled=True,
            soft_edge_exit_max_usd=12.0,
            min_underlying_edge_usd=10.0,
            underlying_gate_enabled=True,
        )
        path.write_text(json.dumps(payload))
        ns = {
            "os": __import__("os"),
            "json": json,
            "math": __import__("math"),
            "STRATEGY_FILE": str(path),
            "_STRATEGY_DEFAULTS": dict(defaults),
            "_STRATEGY_DOC_KEYS": {
                "_comment", "_canonical", "_source_tape", "_notes", "_live_flip",
            },
            "_strat_cache": None,
            "_strat_mtime": 0.0,
            "EXPECTED_TICK_SIZE": "0.01",
            "validate_15m_strategy_coherence": validate_15m_strategy_coherence,
            "probe_spend_usd": probe_spend_usd,
            "console": type("C", (), {"print": staticmethod(lambda *_a, **_k: None)})(),
        }
        exec(compile(load_fn, "buybot.py", "exec"), ns, ns)
        with self.assertRaises(RuntimeError):
            ns["load_strategy"]()

    def test_probe_json_loads_against_defaults(self):
        src = BOT.read_text()
        tree = ast.parse(src)
        defaults = None
        load_fn = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_STRATEGY_DEFAULTS":
                        defaults = ast.literal_eval(node.value)
            if isinstance(node, ast.FunctionDef) and node.name == "load_strategy":
                load_fn = ast.get_source_segment(src, node)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "strategy_buy.json"
        path.write_text(PROBE.read_text())
        ns = {
            "os": __import__("os"),
            "json": json,
            "math": __import__("math"),
            "STRATEGY_FILE": str(path),
            "_STRATEGY_DEFAULTS": dict(defaults),
            "_STRATEGY_DOC_KEYS": {
                "_comment", "_canonical", "_source_tape", "_notes", "_live_flip",
            },
            "_strat_cache": None,
            "_strat_mtime": 0.0,
            "EXPECTED_TICK_SIZE": "0.01",
            "validate_15m_strategy_coherence": validate_15m_strategy_coherence,
            "probe_spend_usd": probe_spend_usd,
            "console": type("C", (), {"print": staticmethod(lambda *_a, **_k: None)})(),
        }
        exec(compile(load_fn, "buybot.py", "exec"), ns, ns)
        cfg = ns["load_strategy"]()
        self.assertIs(cfg["dry_run"], True)
        self.assertIs(cfg["entry_enabled"], False)
        self.assertEqual(cfg["buy_budget"], 5.0)
        self.assertEqual(cfg["buy_threshold"], 0.95)
        self.assertNotIn("max_open_notional", cfg)
        self.assertNotIn("max_daily_notional", cfg)

    def test_leftover_notional_keys_are_unknown(self):
        src = BOT.read_text()
        tree = ast.parse(src)
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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "strategy_buy.json"
        payload = json.loads(PROBE.read_text())
        payload["max_open_notional"] = 5.0
        payload["max_daily_notional"] = 5.0
        path.write_text(json.dumps(payload))
        ns = {
            "os": __import__("os"),
            "json": json,
            "math": __import__("math"),
            "STRATEGY_FILE": str(path),
            "_STRATEGY_DEFAULTS": dict(defaults),
            "_STRATEGY_DOC_KEYS": {
                "_comment", "_canonical", "_source_tape", "_notes", "_live_flip",
            },
            "_strat_cache": None,
            "_strat_mtime": 0.0,
            "EXPECTED_TICK_SIZE": "0.01",
            "validate_15m_strategy_coherence": validate_15m_strategy_coherence,
            "probe_spend_usd": probe_spend_usd,
            "console": type("C", (), {"print": staticmethod(lambda *_a, **_k: None)})(),
        }
        exec(compile(load_fn, "buybot.py", "exec"), ns, ns)
        with self.assertRaises(RuntimeError) as ctx:
            ns["load_strategy"]()
        self.assertIn("unknown strategy keys", str(ctx.exception.__cause__))


class ProbeNowDecisionTests(unittest.TestCase):
    def test_now_helper_logs_ge95_inside_window(self):
        from check_15m_probe_now import _decide

        cfg = json.loads(PROBE.read_text())
        up = {"bid": 0.94, "ask": 0.96}
        dn = {"bid": 0.03, "ask": 0.05}
        out = _decide(cfg, 120.0, up, dn)
        self.assertTrue(out["window_ok"])
        self.assertTrue(out["up_ask_in_band"])
        self.assertTrue(out["would_log_buy"])
        self.assertFalse(out["live_posting"])

    def test_now_helper_skips_midband_and_live_posting(self):
        from check_15m_probe_now import _decide

        cfg = json.loads(PROBE.read_text())
        up = {"bid": 0.90, "ask": 0.92}
        dn = {"bid": 0.07, "ask": 0.09}
        out = _decide(cfg, 120.0, up, dn)
        self.assertFalse(out["up_ask_in_band"])
        self.assertFalse(out["would_log_buy"])
        live = dict(cfg, dry_run=False, entry_enabled=True)
        armed = _decide(live, 120.0, {"bid": 0.94, "ask": 0.96}, {"bid": 0.03, "ask": 0.05})
        self.assertTrue(armed["live_posting"])


class HourlyUntouchedTests(unittest.TestCase):
    def test_hourly_entry_still_uses_hourly_coherence(self):
        src = HOURLY.read_text()
        self.assertIn("validate_hourly_strategy_coherence", src)
        self.assertNotIn("validate_15m_strategy_coherence", src)
        self.assertTrue(HOURLY_JSON.is_file())
        self.assertTrue(HOURLY_EXAMPLE.is_file())
        hourly = json.loads(HOURLY_EXAMPLE.read_text())
        self.assertEqual(hourly["buy_window_min"], 20.0)
        self.assertIn("max_open_notional", hourly)
        self.assertIn("max_daily_notional", hourly)
        self.assertIn("MAX_OPEN_NOTIONAL", src)
        self.assertIn("buy_skip_max_notional", src)


if __name__ == "__main__":
    unittest.main()
