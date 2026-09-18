"""5m persist arm floor is separate from the buy band (no buybot5m import).

Live 18 Sep 01:38–01:39: persist armed at 0.97 (age 0), then REST confirm
``up_ask=null`` cleared both legs. Persist must arm at
``entry_persist_min_price`` (default 0.96) on WS and hold through the rise.
REST confirm must not restart the timer. The buy still needs the 5m band.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from buy.hedge_gate import hedge_persist_ready
from buy.probe_5m import persist_quote_ok
from buy.probe_15m import ask_in_band, entry_may_buy
from buy.strategy_coherence import validate_5m_strategy_coherence

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "buybot5m.py"
HOURLY = ROOT / "buybothourly.py"
PROBE = ROOT / "strategy_buy5m_probe.example.json"
HISTORICAL = ROOT / "strategy_buy5m.example.json"


def _defaults() -> dict:
    import ast

    tree = ast.parse(BOT.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_STRATEGY_DEFAULTS":
                    return ast.literal_eval(node.value)
    raise AssertionError("_STRATEGY_DEFAULTS not found")


class PersistQuoteFloorTests(unittest.TestCase):
    def test_arms_at_persist_min_not_buy_threshold(self):
        self.assertTrue(persist_quote_ok(0.96, 0.96))
        self.assertTrue(persist_quote_ok(0.961, 0.96))
        self.assertFalse(persist_quote_ok(0.959, 0.96))
        self.assertFalse(persist_quote_ok(None, 0.96))
        self.assertTrue(persist_quote_ok(None, 0.96, gui=0.97))
        self.assertFalse(persist_quote_ok(0.95, 0.96, gui=0.99))

    def test_no_upper_cap_inside_persist_zone(self):
        self.assertTrue(persist_quote_ok(0.97, 0.96))
        self.assertTrue(persist_quote_ok(0.99, 0.96))
        self.assertTrue(persist_quote_ok(1.0, 0.96))


class CascadePersistVsBuyBandTests(unittest.TestCase):
    """Ask 96→97: persist holds; buy waits for the band + elapsed persist."""

    BAND_LO = 0.97
    BAND_HI = 0.99
    PERSIST_MIN = 0.96
    PERSIST_S = 1.0

    def _tick(self, ask, now_s, armed_ts):
        eligible = persist_quote_ok(ask, self.PERSIST_MIN)
        fire, armed, why = hedge_persist_ready(
            eligible, now_s=now_s, armed_ts=armed_ts, persist_s=self.PERSIST_S,
        )
        may_buy = entry_may_buy(fire, ask, self.BAND_LO, self.BAND_HI)
        in_band = ask_in_band(ask, self.BAND_LO, self.BAND_HI)
        return fire, armed, why, may_buy, in_band

    def test_arm_at_96_hold_through_rise_buy_only_in_band_after_persist(self):
        t0 = 1000.0
        fire, armed, why, may_buy, in_band = self._tick(0.96, t0, None)
        self.assertEqual((fire, why, may_buy, in_band), (False, "armed", False, False))
        fire, armed, why, may_buy, in_band = self._tick(0.97, t0 + 0.4, armed)
        self.assertEqual((fire, why, may_buy, in_band), (False, "waiting", False, True))
        fire, armed, why, may_buy, in_band = self._tick(0.97, t0 + 1.0, armed)
        self.assertEqual((fire, why, may_buy, in_band), (True, "ready", True, True))

    def test_rest_null_ask_does_not_reset_ws_arm(self):
        """Live 01:39: REST up_ask=null must keep the WS timer."""
        t0 = 50.0
        fire, armed, why = hedge_persist_ready(
            persist_quote_ok(0.97, 0.96),
            now_s=t0, armed_ts=None, persist_s=1.0,
        )
        self.assertEqual((fire, why), (False, "armed"))
        # Missing REST ask: caller skips hedge_persist_ready (no reset).
        fire, armed2, why = hedge_persist_ready(
            persist_quote_ok(0.97, 0.96),
            now_s=t0 + 1.0, armed_ts=armed, persist_s=1.0,
        )
        self.assertEqual(armed2, armed)
        self.assertEqual((fire, why), (True, "ready"))

    def test_printed_rest_ask_below_floor_still_resets(self):
        fire, armed, why = hedge_persist_ready(
            persist_quote_ok(0.97, 0.96),
            now_s=1.0, armed_ts=None, persist_s=1.0,
        )
        self.assertEqual(why, "armed")
        fire, armed, why = hedge_persist_ready(
            persist_quote_ok(0.01, 0.96),
            now_s=1.5, armed_ts=armed, persist_s=1.0,
        )
        self.assertEqual((fire, armed, why), (False, None, "reset"))


class Buybot5mPersistFloorWiringTests(unittest.TestCase):
    def test_defaults_and_probe_json_document_the_knob(self):
        defaults = _defaults()
        self.assertEqual(defaults["entry_persist_min_price"], 0.96)
        self.assertLess(defaults["entry_persist_min_price"], defaults["buy_threshold"])
        data = json.loads(PROBE.read_text())
        self.assertEqual(data["entry_persist_min_price"], 0.96)
        validate_5m_strategy_coherence(defaults)
        validate_5m_strategy_coherence(data)

    def test_bot_arms_persist_from_floor_not_buy_band(self):
        src = BOT.read_text()
        self.assertIn("persist_quote_ok", src)
        self.assertIn("ENTRY_PERSIST_MIN_PRICE", src)
        self.assertIn("entry_persist_min_price", src)
        self.assertIsNone(
            re.search(
                r"entry_book_persist_ready\(\s*cond,\s*buy_leg,\s*True",
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
        self.assertIn("A REST confirm miss must not", src)
        self.assertIn("if up_ask is not None:", src)

    def test_historical_paper_keeps_persist_min_inside_75_90(self):
        data = json.loads(HISTORICAL.read_text())
        self.assertEqual(data["entry_persist_min_price"], 0.75)
        self.assertLessEqual(data["entry_persist_min_price"], data["buy_max_price"])


class HourlyPersistUnchangedTests(unittest.TestCase):
    def test_hourly_still_has_no_persist_min_knob(self):
        src = HOURLY.read_text()
        self.assertNotIn("entry_persist_min_price", src)
        self.assertNotIn("persist_quote_ok", src)


if __name__ == "__main__":
    unittest.main()
