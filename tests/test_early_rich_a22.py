"""Early-rich a22 is an extra last-20m gate; last-10m a22 and b15 stay intact."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from buy.entry_skip import (
    a22_budget_with_orphan_b15,
    applicable_hourly_entry_bands,
    can_arm_hourly_slice,
    early_rich_a22_window_open,
    hourly_entry_persist_s,
    hourly_horizon_min,
    hourly_slice_budget,
    select_hourly_entry_band,
    stamp_early_rich_a22_on_fill,
)
from buy.hedge_gate import hedge_persist_ready

ROOT = Path(__file__).resolve().parents[1]
BOT_HR = ROOT / "buybothourly.py"

# Joel 2026-09-14: last-10m a22, b15 @ 20m, early-rich 97¢ / 90s / 20m.
JOEL = dict(
    a22_window_min=10.0,
    b15_window_min=20.0,
    c5_window_min=0.0,
    b15_min=0.90,
    b15_max=0.94,
    a22_min=0.949,
    high_max=0.99,
    early_rich_enabled=True,
    early_rich_window_min=20.0,
    early_rich_ask_min=0.97,
)


def _bands(minutes_left, **overrides):
    kwargs = dict(JOEL)
    kwargs.update(overrides)
    return applicable_hourly_entry_bands(minutes_left, **kwargs)


def _names(minutes_left, **overrides):
    return [b.name for b in _bands(minutes_left, **overrides)]


def _pick(minutes_left, ask, **overrides):
    return select_hourly_entry_band(ask, _bands(minutes_left, **overrides))


def _defaults():
    tree = ast.parse(BOT_HR.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_STRATEGY_DEFAULTS":
                return ast.literal_eval(node.value)
    raise AssertionError("_STRATEGY_DEFAULTS not found")


class EarlyRichWindowTests(unittest.TestCase):
    def test_ttm_18_is_early_rich_not_normal_a22(self):
        self.assertTrue(
            early_rich_a22_window_open(
                18.0, enabled=True, window_min=20.0, a22_window_min=10.0,
            )
        )
        self.assertFalse(
            early_rich_a22_window_open(
                8.0, enabled=True, window_min=20.0, a22_window_min=10.0,
            )
        )
        self.assertFalse(
            early_rich_a22_window_open(
                21.0, enabled=True, window_min=20.0, a22_window_min=10.0,
            )
        )
        self.assertFalse(
            early_rich_a22_window_open(
                18.0, enabled=False, window_min=20.0, a22_window_min=10.0,
            )
        )
        self.assertFalse(
            early_rich_a22_window_open(
                18.0, enabled=True, window_min=20.0, a22_window_min=0.0,
            )
        )


class EarlyRichBandAndPersistTests(unittest.TestCase):
    def test_a_ttm_18_ask_098_held_90s_can_arm_a22(self):
        names = _names(18)
        self.assertIn("a22", names)
        self.assertIn("b15", names)
        a22 = next(b for b in _bands(18) if b.name == "a22")
        self.assertAlmostEqual(a22.min_price, 0.97)
        self.assertFalse(a22.min_exclusive)
        self.assertAlmostEqual(a22.fak_limit, 0.99)
        band = _pick(18, 0.98)
        self.assertIsNotNone(band)
        self.assertEqual(band.name, "a22")
        persist = hourly_entry_persist_s(
            "a22",
            a22_persist_s=15.0,
            b15_persist_s=20.0,
            early_rich_persist_s=90.0,
            early_rich_active=True,
        )
        self.assertEqual(persist, 90.0)
        fire, armed, why = hedge_persist_ready(
            True, now_s=1000.0, armed_ts=None, persist_s=persist,
        )
        self.assertEqual((fire, why), (False, "armed"))
        fire, armed, why = hedge_persist_ready(
            True, now_s=1089.0, armed_ts=armed, persist_s=persist,
        )
        self.assertEqual((fire, why), (False, "waiting"))
        fire, armed, why = hedge_persist_ready(
            True, now_s=1090.0, armed_ts=armed, persist_s=persist,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")
        ok, skip = can_arm_hourly_slice({}, slice_name="a22", a22_budget=40.0)
        self.assertTrue(ok)
        self.assertIsNone(skip)

    def test_b_ask_dip_below_097_resets_timer(self):
        persist = 90.0
        fire, armed, why = hedge_persist_ready(
            True, now_s=0.0, armed_ts=None, persist_s=persist,
        )
        self.assertEqual(why, "armed")
        fire, armed, why = hedge_persist_ready(
            True, now_s=60.0, armed_ts=armed, persist_s=persist,
        )
        self.assertEqual((fire, why), (False, "waiting"))
        # Flicker below 97¢: no early-rich a22 band match, persist resets.
        self.assertIsNone(_pick(18, 0.965))
        fire, armed, why = hedge_persist_ready(
            False, now_s=61.0, armed_ts=armed, persist_s=persist,
        )
        self.assertEqual((fire, why, armed), (False, "reset", None))
        fire, armed, why = hedge_persist_ready(
            True, now_s=62.0, armed_ts=armed, persist_s=persist,
        )
        self.assertEqual((fire, why), (False, "armed"))
        fire, _, why = hedge_persist_ready(
            True, now_s=151.0, armed_ts=armed, persist_s=persist,
        )
        self.assertEqual((fire, why), (False, "waiting"))
        fire, _, why = hedge_persist_ready(
            True, now_s=152.0, armed_ts=armed, persist_s=persist,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")

    def test_c_ttm_8_ask_095_uses_normal_a22_without_90s_or_97(self):
        self.assertFalse(
            early_rich_a22_window_open(
                8.0, enabled=True, window_min=20.0, a22_window_min=10.0,
            )
        )
        a22 = next(b for b in _bands(8) if b.name == "a22")
        self.assertAlmostEqual(a22.min_price, 0.949)
        self.assertTrue(a22.min_exclusive)
        band = _pick(8, 0.95)
        self.assertIsNotNone(band)
        self.assertEqual(band.name, "a22")
        persist = hourly_entry_persist_s(
            "a22",
            a22_persist_s=15.0,
            b15_persist_s=20.0,
            early_rich_persist_s=90.0,
            early_rich_active=False,
        )
        self.assertEqual(persist, 15.0)
        fire, armed, why = hedge_persist_ready(
            True, now_s=0.0, armed_ts=None, persist_s=persist,
        )
        self.assertEqual(why, "armed")
        fire, _, why = hedge_persist_ready(
            True, now_s=15.0, armed_ts=armed, persist_s=persist,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")

    def test_d_b15_open_at_ttm_18(self):
        self.assertIn("b15", _names(18))
        band = _pick(18, 0.92)
        self.assertIsNotNone(band)
        self.assertEqual(band.name, "b15")
        self.assertAlmostEqual(band.min_price, 0.90)
        self.assertAlmostEqual(band.max_price, 0.94)
        persist = hourly_entry_persist_s(
            "b15",
            a22_persist_s=15.0,
            b15_persist_s=20.0,
            early_rich_persist_s=90.0,
            early_rich_active=True,
        )
        self.assertEqual(persist, 20.0)

    def test_early_rich_a22_does_not_block_b15(self):
        self.assertEqual(_pick(18, 0.98).name, "a22")
        self.assertEqual(_pick(18, 0.92).name, "b15")
        meta = {"t22_bought": True, "bought_token": "up", "pnl_entry_cost": 40.0}
        ok, why = can_arm_hourly_slice(
            meta, slice_name="b15", held_size=42.0, buy_token="up",
            a22_budget=40.0, b15_budget=6.0, market_cap=48.5,
        )
        self.assertTrue(ok)
        self.assertIsNone(why)

    def test_disabled_or_a22_off_does_not_open_early_rich(self):
        self.assertNotIn("a22", _names(18, early_rich_enabled=False))
        self.assertIn("b15", _names(18, early_rich_enabled=False))
        self.assertNotIn("a22", _names(18, a22_window_min=0.0))
        self.assertIsNone(_pick(18, 0.95))

    def test_orphan_b15_still_folds_when_early_rich_a22_fills(self):
        meta = {}
        bud = a22_budget_with_orphan_b15(meta, 160.0, 40.0)
        self.assertEqual(bud, 200.0)
        self.assertAlmostEqual(
            hourly_slice_budget(
                "a22", meta, a22_budget=bud, b15_budget=40.0, market_cap=210.0,
            ),
            200.0,
        )
        after = {"t15_bought": True, "pnl_entry_cost": 40.0, "a22_spent_usd": 0}
        self.assertEqual(a22_budget_with_orphan_b15(after, 160.0, 40.0), 160.0)

    def test_horizon_includes_early_rich_window(self):
        self.assertEqual(
            hourly_horizon_min(10.0, 15.0, 0.0, 10.0, early_rich_window_min=20.0),
            20.0,
        )


class EarlyRichWiringTests(unittest.TestCase):
    def test_defaults_match_joel_knobs(self):
        defaults = _defaults()
        self.assertIs(defaults["early_rich_a22_enabled"], True)
        self.assertEqual(defaults["early_rich_a22_ask_min"], 0.97)
        self.assertEqual(defaults["early_rich_a22_persist_s"], 90.0)
        self.assertEqual(defaults["early_rich_a22_window_min"], 20.0)
        self.assertEqual(defaults["a22_window_min"], 10.0)
        self.assertEqual(defaults["b15_window_min"], 20.0)
        self.assertEqual(defaults["buy_window_min"], 20.0)
        self.assertEqual(defaults["entry_book_persist_s"], 15.0)
        self.assertEqual(defaults["b15_entry_book_persist_s"], 20.0)
        self.assertIs(defaults["early_rich_take_profit_enabled"], True)
        self.assertEqual(defaults["early_rich_take_profit_bid"], 0.99)
        self.assertEqual(defaults["early_rich_take_profit_persist_s"], 5.0)
        # Ordinary full-lock default stays independent of the 99¢ early-rich path.
        self.assertEqual(defaults["take_profit_full_bid"], 0.99)

    def test_stamp_early_rich_on_fill_never_clears(self):
        meta = {}
        self.assertFalse(stamp_early_rich_a22_on_fill(meta, False))
        self.assertNotIn("early_rich_a22", meta)
        self.assertTrue(stamp_early_rich_a22_on_fill(meta, True))
        self.assertIs(meta["early_rich_a22"], True)
        self.assertTrue(stamp_early_rich_a22_on_fill(meta, False))
        self.assertIs(meta["early_rich_a22"], True)

    def test_bot_wires_early_rich_and_logs_lifecycle(self):
        src = BOT_HR.read_text()
        for marker in (
            "early_rich_a22_window_open",
            "hourly_entry_persist_s",
            "early_rich_enabled=",
            "early_rich_window_min=",
            "early_rich_ask_min=",
            "early_rich_a22_armed",
            "early_rich_a22_waiting",
            "early_rich_a22_cleared",
            "early_rich_a22_fired",
            "EARLY_RICH_A22_ENABLED",
            "stamp_early_rich_a22_on_fill(meta, _early_rich_fire)",
            "EARLY_RICH_TAKE_PROFIT_BID",
            "EARLY_RICH_TAKE_PROFIT_PERSIST_S",
            "early_rich_take_profit_full_ready",
            "early_rich_skip_half_take_profit",
        ):
            self.assertIn(marker, src)
        self.assertGreaterEqual(
            src.count("stamp_early_rich_a22_on_fill(meta, _early_rich_fire)"),
            2,
        )
        self.assertIn('"early_rich_take_profit_bid": 0.99', src)
        self.assertIn('"take_profit_full_bid": 0.99', src)


if __name__ == "__main__":
    unittest.main()
