"""15m persist arm floor is separate from the buy band (no buybot import).

Live cascade: ask 95→96→97→98→99. Persist is anti-flash integrity, so it
must arm at ``entry_persist_min_price`` (default 0.95) and hold through the
rise. The buy still requires ``buy_threshold``…``buy_max_price``.
Hourly stays on the old “book already in band” persist arm.
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from pathlib import Path

from buy.hedge_gate import hedge_persist_ready
from buy.probe_15m import (
    ask_in_band,
    entry_may_buy,
    persist_quote_ok,
)
from buy.strategy_coherence import validate_15m_strategy_coherence

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "buybot.py"
HOURLY = ROOT / "buybothourly.py"
HOURLY_EXAMPLE = ROOT / "strategy_buyhourly.example.json"
HOURLY_JSON = ROOT / "strategy_buyhourly.json"
PROBE = ROOT / "strategy_buy15m_probe.example.json"


def _defaults() -> dict:
    tree = ast.parse(BOT.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_STRATEGY_DEFAULTS":
                    return ast.literal_eval(node.value)
    raise AssertionError("_STRATEGY_DEFAULTS not found")


def _load_strategy_ns(payload: dict) -> dict:
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
    path = Path(tmp.name) / "strategy_buy.json"
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
        "probe_spend_usd": lambda budget, max_spend, cap=0.0: min(
            float(budget), float(max_spend), float(cap or budget)
        ),
        "console": type("C", (), {"print": staticmethod(lambda *_a, **_k: None)})(),
        "_tmp": tmp,
    }
    exec(compile(load_fn, "buybot.py", "exec"), ns, ns)
    return ns


class PersistQuoteFloorTests(unittest.TestCase):
    def test_arms_at_persist_min_not_buy_threshold(self):
        self.assertTrue(persist_quote_ok(0.95, 0.95))
        self.assertTrue(persist_quote_ok(0.951, 0.95))
        self.assertFalse(persist_quote_ok(0.949, 0.95))
        self.assertFalse(persist_quote_ok(None, 0.95))
        # GUI is only the fallback when the CLOB ask is missing.
        self.assertTrue(persist_quote_ok(None, 0.95, gui=0.96))
        self.assertFalse(persist_quote_ok(0.94, 0.95, gui=0.96))

    def test_no_upper_cap_inside_persist_zone(self):
        self.assertTrue(persist_quote_ok(0.965, 0.95))
        self.assertTrue(persist_quote_ok(0.99, 0.95))
        self.assertTrue(persist_quote_ok(1.0, 0.95))


class CascadePersistVsBuyBandTests(unittest.TestCase):
    """Ask 95→96.5: persist holds; buy waits for the band + elapsed persist."""

    BAND_LO = 0.965
    BAND_HI = 0.99
    PERSIST_MIN = 0.95
    PERSIST_S = 8.0

    def _tick(self, ask, now_s, armed_ts):
        eligible = persist_quote_ok(ask, self.PERSIST_MIN)
        fire, armed, why = hedge_persist_ready(
            eligible, now_s=now_s, armed_ts=armed_ts, persist_s=self.PERSIST_S,
        )
        may_buy = entry_may_buy(fire, ask, self.BAND_LO, self.BAND_HI)
        in_band = ask_in_band(ask, self.BAND_LO, self.BAND_HI)
        return fire, armed, why, may_buy, in_band

    def test_arm_at_95_hold_through_rise_buy_only_in_band_after_persist(self):
        t0 = 1000.0
        fire, armed, why, may_buy, in_band = self._tick(0.95, t0, None)
        self.assertEqual((fire, why, may_buy, in_band), (False, "armed", False, False))
        self.assertEqual(armed, t0)

        # Still below buy band; timer must not reset solely because price rose.
        fire, armed, why, may_buy, in_band = self._tick(0.96, t0 + 4.0, armed)
        self.assertEqual((fire, why, may_buy, in_band), (False, "waiting", False, False))
        self.assertEqual(armed, t0)

        fire, armed, why, may_buy, in_band = self._tick(0.965, t0 + 7.0, armed)
        self.assertTrue(in_band)
        self.assertFalse(fire)
        self.assertFalse(may_buy)
        self.assertEqual(armed, t0)

        fire, armed, why, may_buy, in_band = self._tick(0.97, t0 + 8.0, armed)
        self.assertTrue(in_band)
        self.assertTrue(fire)
        self.assertTrue(may_buy)
        self.assertEqual(armed, t0)
        self.assertEqual(why, "ready")

        fire, armed, why, may_buy, in_band = self._tick(0.99, t0 + 9.0, armed)
        self.assertTrue(may_buy)
        self.assertEqual(armed, t0)

    def test_reset_when_ask_dips_below_persist_min(self):
        t0 = 2000.0
        fire, armed, why, may_buy, in_band = self._tick(0.95, t0, None)
        self.assertEqual(why, "armed")
        fire, armed, why, may_buy, in_band = self._tick(0.96, t0 + 5.0, armed)
        self.assertEqual(why, "waiting")
        self.assertEqual(armed, t0)

        fire, armed, why, may_buy, in_band = self._tick(0.949, t0 + 6.0, armed)
        self.assertEqual((fire, why, armed, may_buy), (False, "reset", None, False))

        fire, armed, why, may_buy, in_band = self._tick(0.97, t0 + 6.1, armed)
        self.assertEqual(why, "armed")
        self.assertEqual(armed, t0 + 6.1)
        self.assertTrue(in_band)
        self.assertFalse(may_buy)

        fire, armed, why, may_buy, in_band = self._tick(0.97, t0 + 14.1, armed)
        self.assertTrue(may_buy)

    def test_elapsed_persist_still_refuses_buy_below_band(self):
        fire, armed, why = hedge_persist_ready(
            persist_quote_ok(0.96, 0.95),
            now_s=10.0, armed_ts=0.0, persist_s=8.0,
        )
        self.assertTrue(fire)
        self.assertFalse(entry_may_buy(fire, 0.96, 0.965, 0.99))
        self.assertTrue(entry_may_buy(fire, 0.965, 0.965, 0.99))


class BuybotPersistFloorWiringTests(unittest.TestCase):
    def test_defaults_and_probe_json_document_the_knob(self):
        defaults = _defaults()
        self.assertEqual(defaults["entry_persist_min_price"], 0.95)
        self.assertLess(defaults["entry_persist_min_price"], defaults["buy_max_price"])
        data = json.loads(PROBE.read_text())
        self.assertEqual(data["entry_persist_min_price"], 0.95)
        validate_15m_strategy_coherence(defaults)
        validate_15m_strategy_coherence(data)

    def test_bot_arms_persist_from_floor_not_buy_band(self):
        src = BOT.read_text()
        self.assertIn("persist_quote_ok", src)
        self.assertIn("ENTRY_PERSIST_MIN_PRICE", src)
        self.assertIn("entry_may_buy", src)
        self.assertIn("entry_persist_min_price", src)
        # Old path: persist only after a buy-band hit, then pass True.
        self.assertIsNone(
            re.search(
                r"entry_book_persist_ready\(\s*cond,\s*persist_leg,\s*True",
                src,
            )
        )
        self.assertIsNone(
            re.search(
                r"if not \(up_buy or dn_buy\):\s*"
                r"clear_entry_book_persist_leg\(cond, \"up\"\)\s*"
                r"clear_entry_book_persist_leg\(cond, \"down\"\)",
                src,
            )
        )
        self.assertIn("Hourly may get the same later", src)

    def test_load_strategy_accepts_live_band_above_persist_floor(self):
        payload = json.loads(PROBE.read_text())
        payload["buy_threshold"] = 0.965
        payload["buy_max_price"] = 0.99
        payload["entry_persist_min_price"] = 0.95
        ns = _load_strategy_ns(payload)
        self.addCleanup(ns["_tmp"].cleanup)
        cfg = ns["load_strategy"]()
        self.assertEqual(cfg["entry_persist_min_price"], 0.95)
        self.assertEqual(cfg["buy_threshold"], 0.965)

    def test_load_strategy_rejects_persist_min_out_of_range(self):
        payload = json.loads(PROBE.read_text())
        payload["entry_persist_min_price"] = 1.05
        ns = _load_strategy_ns(payload)
        self.addCleanup(ns["_tmp"].cleanup)
        with self.assertRaises(RuntimeError):
            ns["load_strategy"]()


class HourlyPersistUnchangedTests(unittest.TestCase):
    def test_hourly_has_no_persist_min_knob(self):
        src = HOURLY.read_text()
        example = json.loads(HOURLY_EXAMPLE.read_text())
        live_snap = json.loads(HOURLY_JSON.read_text())
        self.assertNotIn("entry_persist_min_price", src)
        self.assertNotIn("persist_quote_ok", src)
        self.assertNotIn("entry_may_buy", src)
        self.assertNotIn("entry_persist_min_price", example)
        self.assertNotIn("entry_persist_min_price", live_snap)
        self.assertIn("entry_book_persist_ready", src)
        self.assertIn("Require entry_book_ok to hold", src)


if __name__ == "__main__":
    unittest.main()
