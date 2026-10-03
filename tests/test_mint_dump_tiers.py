"""Tiered held-dump persistence (sell_dump_tiers). No mintbot import."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from buy.mint_sell import (
    advance_dump_tiers,
    dump_tiers,
    sell_plan_banner,
    validate_dump_tiers,
)
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap

ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
MINT_EXAMPLE = ROOT / "strategy_mint.example.json"
LIVE_TIERS = [[0.40, 6], [0.30, 4], [0.20, 2]]


class TieredHarness:
    """Sell loop on one held-after-scrap bag (Up held) with a settable Up bid."""

    def __init__(self, cfg: dict, ttm: float = 200.0):
        self.ns, self.events, self.fak_calls, self.clock = _dump_harness(_fill_fak)
        self.t0 = self.clock["now"]
        self.bid = {"up": 0.51}
        self.ns["_fetch_books"] = lambda _u, _d, _m: (
            (self.bid["up"], 50.0, [{"price": str(self.bid["up"]), "size": "80"}]),
            (0.49, 50.0, [{"price": "0.49", "size": "80"}]),
        )
        self.cfg = cfg
        self.intent = _held_after_scrap(self.t0 + ttm)
        self.state = {"intents": {"cid-tier": self.intent}}

    def at(self, dt: float, bid: float, cfg: dict | None = None) -> dict:
        self.clock["now"] = self.t0 + dt
        self.bid["up"] = bid
        self.ns["remember_persisted_state"](self.state)
        self.ns["_manage_sells_locked"](cfg or self.cfg, self.state, object())
        return self.intent

    def persist(self, why: str | None = None) -> list:
        rows = [e for e in self.events if e["event"] == "sell_dump_persist"]
        return [e for e in rows if why is None or e["why"] == why]

    def fired(self) -> bool:
        return bool(self.intent.get("sold_dump"))


def _tiered(**extra) -> dict:
    return _dump_cfg(sell_dump_below=0.40, sell_dump_tiers=LIVE_TIERS, **extra)


class EachTierFiresAloneTests(unittest.TestCase):
    def test_40c_tier_needs_six_seconds(self):
        h = TieredHarness(_tiered())
        h.at(0.0, 0.35)
        self.assertEqual([(e["below"], e["persist_s"]) for e in h.persist("armed")], [(0.40, 6.0)])
        h.at(5.9, 0.35)
        self.assertFalse(h.fired())
        self.assertEqual(h.fak_calls, [])
        h.at(6.0, 0.35)
        self.assertTrue(h.fired())
        self.assertEqual(len(h.fak_calls), 1)
        fire = h.persist("fire")
        self.assertEqual(len(fire), 1)
        self.assertEqual(fire[0]["below"], 0.40)
        self.assertEqual(fire[0]["persist_s"], 6.0)
        self.assertAlmostEqual(fire[0]["below_s"], 6.0)
        self.assertTrue(fire[0]["tiered"])

    def test_30c_tier_fires_after_four_seconds(self):
        h = TieredHarness(_tiered())
        h.at(0.0, 0.25)
        self.assertEqual(sorted(e["below"] for e in h.persist("armed")), [0.30, 0.40])
        h.at(3.9, 0.25)
        self.assertFalse(h.fired())
        h.at(4.0, 0.25)
        self.assertTrue(h.fired())
        fire = h.persist("fire")[0]
        self.assertEqual((fire["below"], fire["persist_s"]), (0.30, 4.0))
        self.assertAlmostEqual(fire["below_s"], 4.0)

    def test_20c_tier_fires_after_two_seconds(self):
        h = TieredHarness(_tiered())
        h.at(0.0, 0.15)
        self.assertEqual(len(h.persist("armed")), 3)
        h.at(1.9, 0.15)
        self.assertFalse(h.fired())
        h.at(2.0, 0.15)
        self.assertTrue(h.fired())
        fire = h.persist("fire")[0]
        self.assertEqual((fire["below"], fire["persist_s"]), (0.20, 2.0))
        self.assertEqual(h.intent["sell_dump_leg"], "up")

    def test_bid_at_a_tier_price_does_not_arm_it(self):
        h = TieredHarness(_tiered())
        h.at(0.0, 0.30)
        self.assertEqual([e["below"] for e in h.persist("armed")], [0.40])


class BounceTests(unittest.TestCase):
    def test_015_037_029_resets_30c_and_20c_but_40c_keeps_running(self):
        h = TieredHarness(_tiered())
        h.at(0.0, 0.15)
        self.assertEqual(sorted(e["below"] for e in h.persist("armed")), [0.20, 0.30, 0.40])

        h.at(1.0, 0.37)
        resets = h.persist("reset")
        self.assertEqual(sorted(e["below"] for e in resets), [0.20, 0.30])
        self.assertTrue(all(e["reason"] == "bid_above" for e in resets))
        self.assertAlmostEqual(resets[0]["below_s"], 1.0)
        self.assertEqual(list(h.intent["sell_dump_tier_armed"]), ["0.4000"])
        self.assertEqual(h.intent["sell_dump_tier_armed"]["0.4000"], h.t0)

        h.at(2.0, 0.29)
        rearmed = [e for e in h.persist("armed") if e["below"] == 0.30]
        self.assertEqual(len(rearmed), 2)
        self.assertEqual(h.intent["sell_dump_tier_armed"]["0.3000"], h.t0 + 2.0)
        self.assertNotIn("0.2000", h.intent["sell_dump_tier_armed"])

        # Without the resets the 20c timer would have fired at 2s and 30c at 4s.
        h.at(5.0, 0.29)
        self.assertFalse(h.fired())
        self.assertEqual(h.fak_calls, [])

        h.at(6.0, 0.29)
        self.assertTrue(h.fired())
        fire = h.persist("fire")[0]
        self.assertEqual((fire["below"], fire["persist_s"]), (0.40, 6.0))
        self.assertAlmostEqual(fire["below_s"], 6.0)
        self.assertEqual(len([e for e in h.persist("reset") if e["below"] == 0.40]), 0)

    def test_pure_timer_bounce(self):
        tiers = dump_tiers({"sell_dump_tiers": LIVE_TIERS})
        fired, armed, _ = advance_dump_tiers(True, 0.15, tiers, now_s=0.0, armed=None)
        self.assertIsNone(fired)
        self.assertEqual(armed, {"0.4000": 0.0, "0.3000": 0.0, "0.2000": 0.0})
        fired, armed, changes = advance_dump_tiers(True, 0.37, tiers, now_s=1.0, armed=armed)
        self.assertEqual(armed, {"0.4000": 0.0})
        self.assertEqual([c["why"] for c in changes], ["reset", "reset"])
        fired, armed, _ = advance_dump_tiers(True, 0.29, tiers, now_s=2.0, armed=armed)
        self.assertEqual(armed, {"0.4000": 0.0, "0.3000": 2.0})
        fired, armed, _ = advance_dump_tiers(True, 0.29, tiers, now_s=6.0, armed=armed)
        self.assertEqual(fired["below"], 0.40)
        self.assertEqual(fired["below_s"], 6.0)

    def test_gate_closed_resets_every_tier(self):
        tiers = dump_tiers({"sell_dump_tiers": LIVE_TIERS})
        _, armed, _ = advance_dump_tiers(True, 0.15, tiers, now_s=0.0, armed=None)
        fired, armed, changes = advance_dump_tiers(False, 0.15, tiers, now_s=1.0, armed=armed)
        self.assertIsNone(fired)
        self.assertEqual(armed, {})
        self.assertEqual({c["reason"] for c in changes}, {"gate_closed"})


class GateAndReloadTests(unittest.TestCase):
    def test_240s_gate_still_blocks_tiers(self):
        h = TieredHarness(_tiered(), ttm=300.0)
        h.at(0.0, 0.15)
        h.at(10.0, 0.15)
        self.assertFalse(h.fired())
        self.assertEqual(h.persist(), [])
        self.assertNotIn("sell_dump_tier_armed", h.intent)
        self.assertTrue([e for e in h.events if e["event"] == "sell_dump_time_gated"])
        # Persist only starts once ttm is inside the cutoff.
        h.at(61.0, 0.15)
        self.assertFalse(h.fired())
        h.at(63.0, 0.15)
        self.assertTrue(h.fired())

    def test_tiers_are_read_from_cfg_every_tick(self):
        h = TieredHarness(_dump_cfg(sell_dump_below=0.40))
        h.at(0.0, 0.25)
        self.assertNotIn("sell_dump_tier_armed", h.intent)
        h.at(1.0, 0.25, cfg=_tiered())
        self.assertIn("0.3000", h.intent["sell_dump_tier_armed"])
        h.at(4.9, 0.25, cfg=_tiered())
        self.assertFalse(h.fired())
        h.at(5.0, 0.25, cfg=_tiered())
        self.assertTrue(h.fired())
        self.assertEqual(h.persist("fire")[0]["below"], 0.30)


class FallbackTests(unittest.TestCase):
    def _run_single(self, cfg: dict) -> TieredHarness:
        h = TieredHarness(cfg)
        h.at(0.0, 0.35)
        h.at(1.9, 0.35)
        self.assertFalse(h.fired())
        h.at(2.0, 0.35)
        self.assertTrue(h.fired())
        self.assertNotIn("sell_dump_tier_armed", h.intent)
        whys = [e["why"] for e in h.persist()]
        self.assertEqual(whys, ["armed", "waiting"])
        for row in h.persist():
            self.assertEqual(row["below"], 0.40)
            self.assertNotIn("tiered", row)
        return h

    def test_absent_key_keeps_the_single_timer(self):
        cfg = _dump_cfg(sell_dump_below=0.40)
        self.assertNotIn("sell_dump_tiers", cfg)
        self._run_single(cfg)

    def test_empty_list_keeps_the_single_timer(self):
        self._run_single(_dump_cfg(sell_dump_below=0.40, sell_dump_tiers=[]))

    def test_single_timer_ignores_a_bid_under_a_would_be_lower_tier(self):
        h = TieredHarness(_dump_cfg(sell_dump_below=0.40))
        h.at(0.0, 0.15)
        h.at(1.9, 0.15)
        self.assertFalse(h.fired())
        h.at(2.0, 0.15)
        self.assertTrue(h.fired())


class ParseAndConfigTests(unittest.TestCase):
    def test_parse_sorts_highest_first_and_rejects_bad_rows(self):
        self.assertEqual(
            dump_tiers({"sell_dump_tiers": [[0.2, 2], [0.4, 6], [0.3, 4]]}),
            [(0.4, 6.0), (0.3, 4.0), (0.2, 2.0)],
        )
        for bad in (None, [], "0.4,6", [[0.4]], [[1.2, 3]], [[0.4, -1]], [["x", 2]], [[0.4, float("nan")]]):
            self.assertEqual(dump_tiers({"sell_dump_tiers": bad}), [], bad)

    def test_validation(self):
        validate_dump_tiers({})
        validate_dump_tiers({"sell_dump_tiers": []})
        validate_dump_tiers({"sell_dump_tiers": LIVE_TIERS})
        for bad in ([[0.4]], [[0, 2]], [[0.4, -1]], [[0.4, 6], [0.4, 2]], "0.4"):
            with self.assertRaises(ValueError, msg=bad):
                validate_dump_tiers({"sell_dump_tiers": bad})

    def test_defaults_and_example_leave_tiers_empty(self):
        tree = ast.parse(MINT.read_text(encoding="utf-8"))
        defaults = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in node.targets)
        )
        example = json.loads(MINT_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(defaults["sell_dump_tiers"], [])
        self.assertEqual(example["sell_dump_tiers"], [])

    def test_banner_lists_tiers_only_when_set(self):
        base = {"sell_enabled": True, "sell_dump_below": 0.40, "sell_dump_max_ttm_s": 240}
        self.assertIn("dump held <40c ttm<=240s", sell_plan_banner(base))
        tiered = sell_plan_banner(dict(base, sell_dump_tiers=LIVE_TIERS))
        self.assertIn("dump held <40c/6s,<30c/4s,<20c/2s ttm<=240s", tiered)


if __name__ == "__main__":
    unittest.main()
