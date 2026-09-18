"""5m $5 dry-run probe knobs (no buybot5m.py import)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from buy.entry_skip import validate_late_90_start_s
from buy.probe_5m import (
    ask_in_band,
    in_buy_window_s,
    live_posting_armed,
    probe_live_flip_note,
    probe_spend_usd,
    shares_rail_needed,
    should_evaluate_entries,
)
from buy.strategy_coherence import validate_5m_strategy_coherence

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "buybot5m.py"
PROBE = ROOT / "strategy_buy5m_probe.example.json"
HISTORICAL = ROOT / "strategy_buy5m.example.json"
HOURLY = ROOT / "buybothourly.py"
FIFTEEN = ROOT / "buybot.py"
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


def _extract_load_strategy():
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
    return src, defaults, load_fn


def _load_ns(defaults, load_fn, path: Path) -> dict:
    ns = {
        "os": __import__("os"),
        "json": json,
        "math": __import__("math"),
        "STRATEGY_FILE": str(path),
        "_STRATEGY_DEFAULTS": dict(defaults),
        "_STRATEGY_DOC_KEYS": {
            "_comment", "_canonical", "_source_tape", "_notes", "_live_flip",
            "_vs_15m",
        },
        "_strat_cache": None,
        "_strat_mtime": 0.0,
        "EXPECTED_TICK_SIZE": "0.001",
        "validate_5m_strategy_coherence": validate_5m_strategy_coherence,
        "validate_late_90_start_s": validate_late_90_start_s,
        "probe_spend_usd": probe_spend_usd,
        "console": type("C", (), {"print": staticmethod(lambda *_a, **_k: None)})(),
    }
    exec(compile(load_fn, "buybot5m.py", "exec"), ns, ns)
    return ns


class ProbeMathTests(unittest.TestCase):
    def test_window_is_last_90s(self):
        self.assertTrue(in_buy_window_s(90.0, 90.0))
        self.assertTrue(in_buy_window_s(1.0, 90.0))
        self.assertFalse(in_buy_window_s(91.0, 90.0))
        self.assertFalse(in_buy_window_s(0.0, 90.0))

    def test_ge975_band(self):
        self.assertTrue(ask_in_band(0.975, 0.975, 0.99))
        self.assertTrue(ask_in_band(0.98, 0.975, 0.99))
        self.assertTrue(ask_in_band(0.99, 0.975, 0.99))
        self.assertFalse(ask_in_band(0.974, 0.975, 0.99))
        self.assertFalse(ask_in_band(0.95, 0.975, 0.99))
        self.assertFalse(ask_in_band(1.00, 0.975, 0.99))
        self.assertFalse(ask_in_band(None, 0.975, 0.99))

    def test_spend_cap_is_json_only(self):
        self.assertEqual(probe_spend_usd(5.0, 5.0, 5.0), 5.0)
        self.assertEqual(probe_spend_usd(10.0, 40.0, 40.0), 10.0)
        self.assertEqual(probe_spend_usd(40.0, 40.0, 40.0), 40.0)
        self.assertGreaterEqual(shares_rail_needed(5.0, 0.975), 5.0 / 0.975)
        self.assertGreaterEqual(shares_rail_needed(40.0, 0.975), 40.0 / 0.975)

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
        self.assertIn("polybuybot5m", restart)
        self.assertIn("entry_enabled", arm)


class ProbeJsonTests(unittest.TestCase):
    def test_probe_is_disarmed_five_dollar_ge975(self):
        data = json.loads(PROBE.read_text())
        self.assertIs(data["dry_run"], True)
        self.assertIs(data["entry_enabled"], False)
        self.assertEqual(data["buy_threshold"], 0.975)
        self.assertEqual(data["buy_max_price"], 0.99)
        self.assertEqual(data["buy_start_s"], 90)
        self.assertEqual(data["early_buy_start_s"], 90)
        self.assertEqual(data["late_90_start_s"], 0)
        self.assertEqual(data["min_underlying_edge_usd"], 10.0)
        self.assertEqual(data["buy_budget"], 5.0)
        self.assertEqual(data["late_buy_budget"], 5.0)
        self.assertEqual(data["buy_max_spend"], 5.0)
        self.assertEqual(data["market_spend_cap"], 5.0)
        self.assertEqual(data["entry_book_persist_s"], 5.0)
        self.assertEqual(data["entry_persist_min_price"], 0.96)
        self.assertEqual(data["hedge_persist_s"], 1.0)
        self.assertEqual(data["hedge_dump_persist_s"], 2.0)
        self.assertEqual(data["hedge_threshold"], 0.50)
        self.assertEqual(data["hedge_require_ask_max"], 0.52)
        self.assertEqual(data["hedge_toxic_bid_max"], 0.40)
        self.assertIs(data["hedge_require_oracle"], True)
        self.assertIs(data["hedge_dump_ignore_oracle"], False)
        self.assertIs(data["hedge_dump_require_tight"], True)
        self.assertEqual(data["hedge_dump_ignore_spread_ask_max"], 0.60)
        self.assertIs(data["hedge_dump_require_fresh_book"], True)
        self.assertIs(data["hedge_edge_collapse_allows_dump"], False)
        self.assertEqual(data["hedge_dump_max_favor_edge_usd"], 0.0)
        self.assertIs(data["hedge_dump_ladder_enabled"], True)
        self.assertEqual(data["hedge_dump_late_sweep_ttm_s"], 15.0)
        self.assertEqual(data["tick_size"], "0.001")
        validate_5m_strategy_coherence(data)
        self.assertFalse(live_posting_armed(data["dry_run"], data["entry_enabled"]))

    def test_historical_example_stays_last120_paper(self):
        five = json.loads(HISTORICAL.read_text())
        self.assertEqual(five["buy_start_s"], 120)
        self.assertEqual(five["buy_threshold"], 0.75)
        self.assertEqual(five["buy_max_price"], 0.90)
        self.assertEqual(five["buy_budget"], 2.5)
        self.assertEqual(five["entry_persist_min_price"], 0.75)
        self.assertIs(five["dry_run"], True)
        self.assertIs(five["entry_enabled"], False)


class Coherence5mTests(unittest.TestCase):
    def test_rejects_window_outside_5m_clock(self):
        with self.assertRaises(ValueError) as ctx:
            validate_5m_strategy_coherence({"buy_start_s": 301, "buy_budget": 5.0})
        self.assertIn("buy_start_s", str(ctx.exception))

    def test_rejects_persist_longer_than_window(self):
        with self.assertRaises(ValueError) as ctx:
            validate_5m_strategy_coherence({
                "buy_start_s": 90,
                "entry_book_persist_s": 91,
                "buy_budget": 5.0,
            })
        self.assertIn("entry_book_persist_s", str(ctx.exception))

    def test_rejects_persist_min_above_buy_max(self):
        with self.assertRaises(ValueError) as ctx:
            validate_5m_strategy_coherence({
                "buy_start_s": 90,
                "buy_budget": 5.0,
                "buy_max_price": 0.90,
                "entry_persist_min_price": 0.96,
            })
        self.assertIn("entry_persist_min_price", str(ctx.exception))

    def test_rejects_spend_cap_below_budget(self):
        with self.assertRaises(ValueError) as ctx:
            validate_5m_strategy_coherence({
                "buy_start_s": 90,
                "buy_budget": 5.0,
                "market_spend_cap": 2.0,
            })
        self.assertIn("market_spend_cap", str(ctx.exception))

    def test_probe_window_and_persist_load(self):
        validate_5m_strategy_coherence({
            "buy_start_s": 90,
            "entry_book_persist_s": 5.0,
            "buy_budget": 5.0,
            "market_spend_cap": 5.0,
        })


class Buybot5mWiringTests(unittest.TestCase):
    def test_defaults_match_probe_rails(self):
        defaults = _defaults()
        self.assertEqual(defaults["buy_threshold"], 0.975)
        self.assertEqual(defaults["buy_max_price"], 0.99)
        self.assertEqual(defaults["buy_start_s"], 90)
        self.assertEqual(defaults["early_buy_start_s"], 90)
        self.assertEqual(defaults["min_underlying_edge_usd"], 10.0)
        self.assertEqual(defaults["buy_budget"], 5.0)
        self.assertEqual(defaults["late_buy_budget"], 5.0)
        self.assertEqual(defaults["market_spend_cap"], 5.0)
        self.assertEqual(defaults["entry_book_persist_s"], 5.0)
        self.assertEqual(defaults["entry_persist_min_price"], 0.96)
        self.assertEqual(defaults["hedge_persist_s"], 1.0)
        self.assertEqual(defaults["hedge_dump_persist_s"], 2.0)
        self.assertIs(defaults["hedge_require_oracle"], True)
        self.assertIs(defaults["hedge_dump_ignore_oracle"], False)
        self.assertIs(defaults["hedge_dump_require_tight"], True)
        self.assertEqual(defaults["hedge_dump_ignore_spread_ask_max"], 0.60)
        self.assertIs(defaults["hedge_dump_require_fresh_book"], True)
        self.assertIs(defaults["hedge_edge_collapse_allows_dump"], False)
        self.assertEqual(defaults["hedge_dump_max_favor_edge_usd"], 0.0)
        self.assertIs(defaults["hedge_dump_ladder_enabled"], True)
        self.assertEqual(defaults["hedge_dump_late_sweep_ttm_s"], 15.0)
        self.assertIs(defaults["dry_run"], True)
        self.assertIs(defaults["entry_enabled"], False)
        validate_5m_strategy_coherence(defaults)

    def test_bot_wires_coherence_and_dump_harden(self):
        src = BOT.read_text()
        self.assertIn("validate_5m_strategy_coherence", src)
        self.assertIn("should_evaluate_entries(DRY_RUN, ENTRY_ENABLED)", src)
        self.assertIn("probe_spend_usd", src)
        self.assertIn("entry_book_persist_ready", src)
        self.assertIn("persist_quote_ok", src)
        self.assertIn("ENTRY_PERSIST_MIN_PRICE", src)
        self.assertIn("entry_persist_min_price", src)
        self.assertIn("hold_while_oracle_agrees", src)
        self.assertIn("hedge_persist_ready", src)
        self.assertIn("dump_tight_book_hold_reason(", src)
        self.assertIn("hedge_skip_dump_book", src)
        self.assertIn('"hedge_dump_ignore_oracle": False', src)
        self.assertIn('"hedge_dump_require_tight": True', src)
        self.assertIn("HEDGE_DUMP_REQUIRE_TIGHT", src)
        self.assertIn("HEDGE_DUMP_REQUIRE_FRESH_BOOK", src)
        self.assertIn("HEDGE_EDGE_COLLAPSE_ALLOWS_DUMP", src)
        self.assertIn("hedge_skip_dump_oracle_favor", src)
        self.assertIn("not (do_dump and HEDGE_DUMP_IGNORE_ORACLE)", src)
        self.assertIn("POLY_BUY5M_STRATEGY", src)
        self.assertIn("build_dump_exit_price_ladder", src)
        self.assertIn("SOURCE_CHAINLINK", src)
        self.assertIn("ptb_chainlink_buy5m.json", src)
        self.assertNotIn("SOURCE_TWAP_30", src)
        self.assertNotIn("SOURCE_TWAP_60", src)

    def test_load_strategy_rejects_persist_longer_than_window(self):
        _src, defaults, load_fn = _extract_load_strategy()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "strategy_buy5m.json"
        payload = json.loads(PROBE.read_text())
        payload["entry_book_persist_s"] = 91.0
        path.write_text(json.dumps(payload))
        ns = _load_ns(defaults, load_fn, path)
        with self.assertRaises(RuntimeError):
            ns["load_strategy"]()

    def test_probe_json_loads_against_defaults(self):
        _src, defaults, load_fn = _extract_load_strategy()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "strategy_buy5m.json"
        path.write_text(PROBE.read_text())
        ns = _load_ns(defaults, load_fn, path)
        cfg = ns["load_strategy"]()
        self.assertIs(cfg["dry_run"], True)
        self.assertIs(cfg["entry_enabled"], False)
        self.assertEqual(cfg["buy_budget"], 5.0)
        self.assertEqual(cfg["buy_threshold"], 0.975)
        self.assertEqual(cfg["buy_start_s"], 90)
        self.assertIs(cfg["hedge_dump_ignore_oracle"], False)
        self.assertIs(cfg["hedge_dump_require_tight"], True)
        self.assertIs(cfg["hedge_dump_require_fresh_book"], True)
        self.assertIs(cfg["hedge_edge_collapse_allows_dump"], False)

    def test_historical_json_still_loads(self):
        _src, defaults, load_fn = _extract_load_strategy()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "strategy_buy5m.json"
        path.write_text(HISTORICAL.read_text())
        ns = _load_ns(defaults, load_fn, path)
        cfg = ns["load_strategy"]()
        self.assertEqual(cfg["buy_threshold"], 0.75)
        self.assertEqual(cfg["buy_start_s"], 120)
        self.assertIs(cfg["hedge_dump_require_fresh_book"], True)


class ProbeNowDecisionTests(unittest.TestCase):
    def test_now_helper_logs_ge975_inside_window(self):
        from check_5m_probe_now import _decide

        cfg = json.loads(PROBE.read_text())
        up = {"bid": 0.97, "ask": 0.98}
        dn = {"bid": 0.02, "ask": 0.03}
        out = _decide(cfg, 60.0, up, dn)
        self.assertTrue(out["window_ok"])
        self.assertTrue(out["up_ask_in_band"])
        self.assertTrue(out["would_log_buy"])
        self.assertFalse(out["live_posting"])

    def test_now_helper_skips_midband_and_outside_window(self):
        from check_5m_probe_now import _decide

        cfg = json.loads(PROBE.read_text())
        up = {"bid": 0.90, "ask": 0.92}
        dn = {"bid": 0.07, "ask": 0.09}
        out = _decide(cfg, 60.0, up, dn)
        self.assertFalse(out["up_ask_in_band"])
        self.assertFalse(out["would_log_buy"])
        late = _decide(cfg, 91.0, {"bid": 0.97, "ask": 0.98}, {"bid": 0.02, "ask": 0.03})
        self.assertFalse(late["window_ok"])
        self.assertFalse(late["would_log_buy"])
        live = dict(cfg, dry_run=False, entry_enabled=True)
        armed = _decide(live, 60.0, {"bid": 0.97, "ask": 0.98}, {"bid": 0.02, "ask": 0.03})
        self.assertTrue(armed["live_posting"])


class SiblingUntouchedTests(unittest.TestCase):
    def test_hourly_and_15m_entry_untouched(self):
        hourly_src = HOURLY.read_text()
        fifteen_src = FIFTEEN.read_text()
        self.assertIn("validate_hourly_strategy_coherence", hourly_src)
        self.assertNotIn("validate_5m_strategy_coherence", hourly_src)
        self.assertIn("validate_15m_strategy_coherence", fifteen_src)
        self.assertNotIn("validate_5m_strategy_coherence", fifteen_src)
        self.assertTrue(HOURLY_JSON.is_file())
        self.assertTrue(HOURLY_EXAMPLE.is_file())
        hourly = json.loads(HOURLY_EXAMPLE.read_text())
        self.assertEqual(hourly["buy_window_min"], 20.0)


if __name__ == "__main__":
    unittest.main()
