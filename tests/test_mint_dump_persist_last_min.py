"""Optional last-minute persist for the held-leg dump."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from buy.mint_gas import validate_mint_gas
from buy.mint_redeem import validate_redeem
from buy.mint_sell import (
    DEFAULT_SELL_KNOBS,
    dump_persist_knobs,
    effective_dump_persist_s,
    effective_loser_persist_s,
    sell_plan_banner,
)
from buy.mint_sequence import validate_seq
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_only_ops import _assign, _fn

ROOT = Path(__file__).resolve().parents[1]
MINT_EXAMPLE = ROOT / "strategy_mint.example.json"
END = 10_000.0


def _eff(ttm: float, cfg: dict) -> float:
    persist, last_min, window = dump_persist_knobs(cfg)
    return effective_dump_persist_s(
        now_s=END - ttm,
        end_ts=END,
        persist_s=persist,
        last_min_s=last_min,
        last_min_window_s=window,
    )


class EffectiveDumpPersistTests(unittest.TestCase):
    def test_defaults_match_sell_dump_persist_s_at_any_ttm(self):
        for cfg in ({}, {"sell_dump_persist_s": 3.0}, {"sell_dump_persist_last_min_s": 0.5}):
            base = cfg.get("sell_dump_persist_s", 2.0)
            for ttm in (1, 30, 60, 120, 121, 200, 900):
                self.assertEqual(_eff(ttm, cfg), base, (cfg, ttm))

    def test_window_switches_clock_inclusive_at_the_boundary(self):
        cfg = {
            "sell_dump_persist_s": 2.0,
            "sell_dump_persist_last_min_s": 1.0,
            "sell_dump_persist_last_min_window_s": 120.0,
        }
        for ttm in (120, 60, 1):
            self.assertEqual(_eff(ttm, cfg), 1.0, ttm)
        for ttm in (121, 200):
            self.assertEqual(_eff(ttm, cfg), 2.0, ttm)

    def test_missing_or_null_last_min_falls_back_to_sell_dump_persist_s(self):
        for extra in ({}, {"sell_dump_persist_last_min_s": None}):
            cfg = dict(extra, sell_dump_persist_s=2.5, sell_dump_persist_last_min_window_s=120.0)
            self.assertEqual(dump_persist_knobs(cfg), (2.5, 2.5, 120.0))
            self.assertEqual(_eff(60, cfg), 2.5)

    def test_explicit_zero_last_min_is_respected(self):
        cfg = {"sell_dump_persist_last_min_s": 0, "sell_dump_persist_last_min_window_s": 120}
        self.assertEqual(_eff(60, cfg), 0.0)

    def test_ended_market_falls_back_to_base_persist(self):
        self.assertIsNone(
            effective_loser_persist_s(
                now_s=END, end_ts=END, persist_s=2.0, last_min_s=1.0, last_min_window_s=120.0
            )
        )
        cfg = {"sell_dump_persist_last_min_s": 1.0, "sell_dump_persist_last_min_window_s": 120.0}
        self.assertEqual(_eff(0, cfg), 2.0)
        self.assertEqual(_eff(-5, cfg), 2.0)

    def test_defaults_dicts_keep_the_feature_off(self):
        for defaults in (DEFAULT_SELL_KNOBS, _assign("DEFAULTS")):
            self.assertIsNone(defaults["sell_dump_persist_last_min_s"])
            self.assertEqual(defaults["sell_dump_persist_last_min_window_s"], 0.0)
            self.assertEqual(defaults["sell_dump_persist_s"], 2.0)
        example = json.loads(MINT_EXAMPLE.read_text())
        self.assertEqual(example["sell_dump_persist_last_min_window_s"], 0)
        self.assertEqual(example["sell_dump_persist_last_min_s"], example["sell_dump_persist_s"])


class ScrapPersistUnchangedTests(unittest.TestCase):
    def test_scrap_persist_values(self):
        for ttm, want in ((200, 5.0), (61, 5.0), (60, 2.0), (1, 2.0)):
            got = effective_loser_persist_s(
                now_s=END - ttm, end_ts=END, persist_s=5.0, last_min_s=2.0, last_min_window_s=60.0
            )
            self.assertEqual(got, want, ttm)
        self.assertIsNone(
            effective_loser_persist_s(
                now_s=END, end_ts=END, persist_s=5.0, last_min_s=2.0, last_min_window_s=60.0
            )
        )

    def test_scrap_defaults(self):
        for defaults in (DEFAULT_SELL_KNOBS, _assign("DEFAULTS")):
            self.assertEqual(defaults["sell_persist_s"], 5.0)
            self.assertEqual(defaults["sell_persist_last_min_s"], 2.0)
            self.assertEqual(defaults["sell_persist_last_min_window_s"], 60.0)


class ValidationTests(unittest.TestCase):
    def test_negatives_rejected_and_zero_accepted(self):
        validate = _fn(
            "validate_strategy",
            {
                "validate_mint_gas": validate_mint_gas,
                "validate_seq": validate_seq,
                "validate_redeem": validate_redeem,
            },
        )
        base = _assign("DEFAULTS")
        validate(base)
        for key in ("sell_dump_persist_last_min_s", "sell_dump_persist_last_min_window_s"):
            validate(dict(base, **{key: 0}))
            with self.assertRaises(ValueError) as caught:
                validate(dict(base, **{key: -1}))
            self.assertIn(key, str(caught.exception))


class BannerTests(unittest.TestCase):
    CFG = {
        "sell_enabled": True,
        "sell_dump_enabled": True,
        "sell_dump_below": 0.4,
        "sell_dump_persist_s": 2.0,
    }

    def test_banner_shows_last_min_dump_persist_when_enabled(self):
        cfg = dict(
            self.CFG,
            sell_dump_persist_last_min_s=1.0,
            sell_dump_persist_last_min_window_s=120.0,
        )
        self.assertIn("dump held <40c persist 2s (1s last 120s)", sell_plan_banner(cfg))

    def test_banner_unchanged_when_disabled(self):
        text = sell_plan_banner(dict(self.CFG, sell_dump_persist_last_min_s=1.0))
        self.assertNotIn("persist", text.split("dump held")[1])


class DumpFireWithoutResetTests(unittest.TestCase):
    def _tick(self, ns, cfg, intent):
        state = {"intents": {"cid-dump": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())

    def test_arm_on_2s_clock_fires_on_1s_clock_without_reset(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 125.0
        intent = _held_after_scrap(end)
        cfg = _dump_cfg(
            sell_dump_persist_last_min_s=1.0,
            sell_dump_persist_last_min_window_s=120.0,
        )
        self._tick(ns, cfg, intent)
        self.assertEqual(intent["sell_dump_armed_at"], clock["now"])
        self.assertEqual(fak_calls, [])
        persist = [e for e in events if e["event"] == "sell_dump_persist"]
        self.assertEqual(persist[-1]["persist_s"], 2.0)

        # Move to ttm 119 with an arm that is 1.1s old: ready on 1s, not on 2s.
        clock["now"] = end - 119.0
        intent["sell_dump_armed_at"] = clock["now"] - 1.1
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertTrue(intent.get("sold_dump"))

    def test_same_elapsed_does_not_fire_with_feature_off(self):
        ns, _events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 119.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 1.1)
        self._tick(ns, _dump_cfg(), intent)
        self.assertEqual(fak_calls, [])
        self.assertEqual(intent["sell_dump_armed_at"], clock["now"] - 1.1)

    def test_armed_ts_kept_across_the_switch(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 121.0
        intent = _held_after_scrap(end)
        cfg = _dump_cfg(
            sell_dump_persist_last_min_s=1.0,
            sell_dump_persist_last_min_window_s=120.0,
        )
        self._tick(ns, cfg, intent)
        armed = intent["sell_dump_armed_at"]
        clock["now"] += 0.5  # ttm 120.5, still on the 2s clock
        self._tick(ns, cfg, intent)
        self.assertEqual(intent["sell_dump_armed_at"], armed)
        self.assertEqual(fak_calls, [])
        clock["now"] += 1.0  # ttm 119.5, elapsed 1.5 >= 1.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertEqual(
            [e["persist_s"] for e in events if e["event"] == "sell_dump_persist"],
            [2.0, 2.0],
        )


if __name__ == "__main__":
    unittest.main()
