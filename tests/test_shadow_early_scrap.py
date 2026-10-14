"""Paper early-scrap math. No mintbot import and no orders."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from buy.shadow_early_scrap import (
    ASSUMPTION,
    build_bag_result,
    close_bag,
    dump_share_delta,
    estimate_bid_walk,
    note_resolution,
    observe,
    reset_for_tests,
    set_log_path,
    set_logger,
    shadow_enabled,
    winning_side,
)


def _cfg(**extra) -> dict:
    cfg = {
        "shadow_early_scrap_enabled": True,
        "shadow_early_threshold": 0.20,
        "shadow_early_min_px": 0.15,
        "shadow_early_opposite_min": 0.75,
        "shadow_early_fraction": 0.5,
        "sell_scrap_max_ttm_s": 360.0,
        "sell_persist_s": 0.0,
        "sell_persist_last_min_s": 2.0,
        "sell_persist_last_min_window_s": 90.0,
    }
    cfg.update(extra)
    return cfg


class ShadowEarlyScrapTests(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "shadow_early_scrap.jsonl"
        set_log_path(self.path)
        set_logger(None)

    def test_disabled_does_nothing(self):
        self.assertFalse(shadow_enabled({}))
        events = observe(
            {"shadow_early_scrap_enabled": False},
            condition_id="c",
            slug="s",
            now_s=10.0,
            end_ts=400.0,
            ttm_s=300.0,
            shares=200.0,
            up_bid=0.18,
            dn_bid=0.82,
            up_bids=[{"price": "0.18", "size": "10"}],
            dn_bids=[],
        )
        self.assertEqual(events, [])
        self.assertFalse(self.path.exists())

    def test_walk_stops_at_the_floor(self):
        walked = estimate_bid_walk(
            [
                {"price": "0.18", "size": "10"},
                {"price": "0.16", "size": "100"},
                {"price": "0.10", "size": "500"},
            ],
            25,
            0.15,
        )
        self.assertAlmostEqual(walked["est_filled_size"], 25.0)
        self.assertAlmostEqual(walked["est_proceeds"], 4.2)
        self.assertAlmostEqual(walked["est_vwap"], 0.168)
        self.assertIn("no queue", walked["est_note"])
        thin = estimate_bid_walk(
            [{"price": "0.16", "size": "4"}],
            25,
            0.15,
        )
        self.assertAlmostEqual(thin["est_filled_size"], 4.0)
        self.assertAlmostEqual(thin["est_proceeds"], 0.64)

    def test_dump_overlap_only_when_the_shadow_leg_is_sold_later(self):
        same, _px, cash = dump_share_delta(
            shadow_leg="up",
            shadow_size=25,
            live_leg="up",
            live_size=25,
            dump_leg="dn",
            dump_size=50,
            dump_px=0.40,
            kept_leg="up",
            kept_size=25,
            kept_px=0.02,
        )
        self.assertEqual(same, 0.0)
        self.assertEqual(cash, 0.0)
        other, px, cash = dump_share_delta(
            shadow_leg="up",
            shadow_size=25,
            live_leg="dn",
            live_size=25,
            dump_leg="up",
            dump_size=50,
            dump_px=0.40,
            kept_leg=None,
            kept_size=0,
            kept_px=None,
        )
        self.assertAlmostEqual(other, 25.0)
        self.assertAlmostEqual(px, 0.40)
        self.assertAlmostEqual(cash, 10.0)

    def test_resolution_fills_a_pending_winner(self):
        end = 1_000.0
        observe(
            _cfg(),
            condition_id="c",
            slug="s",
            now_s=end - 300.0,
            end_ts=end,
            ttm_s=300.0,
            shares=50.0,
            up_bid=0.18,
            dn_bid=0.82,
            up_bids=[{"price": "0.18", "size": "40"}],
            dn_bids=[{"price": "0.82", "size": "40"}],
        )
        logged: list = []
        set_logger(lambda event, **fields: logged.append({"event": event, **fields}))
        note_resolution("c", num_up=0, num_dn=1, slug="s", now_s=end)
        closed = close_bag(
            _cfg(),
            condition_id="c",
            slug="s",
            intent={"sold_loser": False},
            now_s=end + 1,
        )
        self.assertEqual(closed[0][1]["winning_side"], "dn")
        self.assertEqual(closed[0][1]["winning_side_reason"], "payout_numerators")
        self.assertEqual(logged, [])
        self.assertEqual(winning_side(1, 0), "up")
        self.assertIsNone(winning_side(1, 1))
        note_resolution("c", num_up=1, num_dn=0, slug="s", now_s=end + 80)
        self.assertEqual(logged[0]["event"], "shadow_resolution")
        self.assertEqual(logged[0]["winning_side"], "up")
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["event"], "shadow_bag_result")
        self.assertEqual(json.loads(lines[1])["event"], "shadow_resolution")
        self.assertIn("est_scrap_delta_usd", json.loads(lines[0]))
        self.assertIn(ASSUMPTION, json.loads(lines[0])["assumption"])

    def test_build_result_marks_estimates(self):
        rec = {
            "mode": "early",
            "filled": True,
            "winning_side": None,
            "fill": {
                "leg": "up",
                "bid": 0.18,
                "size": 25.0,
                "est_vwap": 0.18,
                "est_proceeds": 4.5,
                "est_filled_size": 25.0,
            },
        }
        fields = build_bag_result(
            rec,
            {"sold_loser": True, "sold_leg": "up", "sell_filled": 25, "sell_fill_px": 0.03},
            condition_id="c",
            slug="s",
        )
        self.assertAlmostEqual(fields["est_scrap_delta_usd"], 3.75)
        self.assertEqual(fields["winning_side_reason"], "pending_resolution")
        self.assertTrue(fields["assumption"].startswith("est_scrap_delta_usd"))
