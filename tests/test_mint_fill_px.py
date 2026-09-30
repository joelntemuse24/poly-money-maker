"""Recorded scrap / dump price is the average fill, not the order limit."""

from __future__ import annotations

import unittest

from buy.mint_sell import record_fill_px, recorded_fill_px
from test_mint_cpu import (
    _dump_cfg,
    _dump_harness,
    _fill_fak,
    _held_after_scrap,
    _open_scrap_bag,
    _scrap_cfg,
    _scrap_harness,
)


def _tick(ns, cfg, intent, cid="cid-px"):
    state = {"intents": {cid: intent}}
    ns["remember_persisted_state"](state)
    ns["_manage_sells_locked"](cfg, state, object())
    return intent


class RecordFillPxTests(unittest.TestCase):
    def test_share_weighted_average_across_calls(self):
        intent: dict = {}
        self.assertEqual(record_fill_px(intent, "px", [(20.0, 0.03), (10.0, 0.02)]), 0.0267)
        self.assertEqual(intent["px_shares"], 30.0)
        self.assertEqual(record_fill_px(intent, "px", [(20.0, 0.01)]), 0.02)
        self.assertEqual(intent["px_shares"], 50.0)

    def test_unpriced_or_empty_fills_leave_the_record_alone(self):
        intent: dict = {}
        self.assertIsNone(record_fill_px(intent, "px", [(20.0, None)]))
        self.assertNotIn("px", intent)
        self.assertIsNone(record_fill_px(intent, "px", []))
        record_fill_px(intent, "px", [(10.0, 0.02), (40.0, None), (0.0, 0.5)])
        self.assertEqual(intent["px"], 0.02)
        self.assertEqual(intent["px_shares"], 10.0)

    def test_reader_prefers_fill_then_falls_back_to_limit(self):
        self.assertEqual(recorded_fill_px({"f": 0.025, "l": 0.01}, "f", "l"), 0.025)
        self.assertEqual(recorded_fill_px({"l": 0.01}, "f", "l"), 0.01)
        self.assertIsNone(recorded_fill_px({}, "f", "l"))
        self.assertEqual(recorded_fill_px({"f": "bad", "l": 0.01}, "f", "l"), 0.01)


class LoserScrapFillPxTests(unittest.TestCase):
    def test_floor_sweep_records_avg_fill_and_keeps_the_limit(self):
        ns, events, fak_calls, clock, _book = _scrap_harness()
        end = clock["now"] + 200.0
        intent = _open_scrap_bag(end, sell_loser_armed_at=clock["now"] - 10.0)
        cfg = _scrap_cfg(sell_floor=0.01, sell_scrap_max_ttm_s=0)
        _tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.01)
        self.assertTrue(intent.get("sold_loser"))
        self.assertEqual(intent["sell_limit"], 0.01)
        self.assertEqual(intent["sell_fill_px"], 0.015)
        self.assertEqual(intent["sell_fill_px_shares"], 50.0)
        done = [row for row in events if row["event"] == "sell_loser_done"]
        self.assertEqual(done[0]["avg_px"], 0.015)

    def test_ladder_mode_records_avg_fill_not_the_last_rung(self):
        ns, _events, fak_calls, clock, _book = _scrap_harness()
        end = clock["now"] + 200.0
        intent = _open_scrap_bag(end, sell_loser_armed_at=clock["now"] - 10.0)
        cfg = _scrap_cfg(
            sell_floor=0.01, sell_scrap_max_ttm_s=0, sell_scrap_sweep_enabled=False,
        )
        _tick(ns, cfg, intent)
        # Ladder mode clips to the 40 shown at the rung, so this tick is partial.
        self.assertEqual([(row["price"], row["size"]) for row in fak_calls], [(0.01, 40.0)])
        self.assertEqual(intent["sell_filled"], 40.0)
        self.assertEqual(intent["sell_limit"], 0.01)
        self.assertEqual(intent["sell_fill_px"], 0.015)
        self.assertEqual(intent["sell_fill_px_shares"], 40.0)

    def test_bag_risk_scrap_px_is_the_fill_after_a_restart(self):
        ns, events, _fak, clock, _book = _scrap_harness()
        end = clock["now"] + 40.0
        intent = _open_scrap_bag(
            end,
            sold_loser=True,
            sold_leg="dn",
            sold_loser_at=end - 40.0,
            sell_limit=0.01,
            sell_fill_px=0.025,
        )
        cfg = _scrap_cfg(sell_scrap_max_ttm_s=600.0)
        _tick(ns, cfg, intent)
        clock["now"] = end + 1.0
        _tick(ns, cfg, intent)
        risks = [row for row in events if row["event"] == "bag_risk"]
        self.assertEqual(len(risks), 1)
        self.assertTrue(risks[0]["partial"])
        self.assertAlmostEqual(risks[0]["scrap_avg_px"], 0.025)


class CheapWinnerUsesFillTests(unittest.TestCase):
    def _reason(self, **cfg_extra):
        ns, _events, _fak, clock = _dump_harness(_fill_fak)
        intent = _held_after_scrap(
            clock["now"] + 500.0, sell_limit=0.01, sell_fill_px=0.02,
        )
        cfg = _dump_cfg(sell_dump_enabled=False, **cfg_extra)
        _tick(ns, cfg, intent)
        return intent.get("sell_winner_cheap_reason")

    def test_real_fill_opens_the_cheap_winner_when_floor_would_not(self):
        # Floor 1c + 99c = 1.00 is flat; the real 2c fill + 99c beats mint.
        self.assertEqual(
            self._reason(sell_winner_cheap_if_loser_le=0.03, sell_winner_min_cheap=0.99),
            "positive_edge",
        )

    def test_real_fill_above_the_gate_closes_it(self):
        self.assertEqual(
            self._reason(sell_winner_cheap_if_loser_le=0.015, sell_winner_min_cheap=0.99),
            "loser_above_cheap_gate",
        )

    def test_live_gate_minus_one_is_closed_either_way(self):
        self.assertEqual(
            self._reason(sell_winner_cheap_if_loser_le=-1.0, sell_winner_min_cheap=0.999),
            "loser_above_cheap_gate",
        )

    def test_old_state_without_a_fill_still_uses_the_limit(self):
        ns, _events, _fak, clock = _dump_harness(_fill_fak)
        intent = _held_after_scrap(clock["now"] + 500.0, sell_limit=0.01)
        cfg = _dump_cfg(
            sell_dump_enabled=False,
            sell_winner_cheap_if_loser_le=0.03,
            sell_winner_min_cheap=0.99,
        )
        _tick(ns, cfg, intent)
        self.assertEqual(intent.get("sell_winner_cheap_reason"), "flat_or_negative_edge")


class DumpFillPxTests(unittest.TestCase):
    def test_dump_records_avg_fill_and_bag_risk_reports_it(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)

        def priced_fak(token_id, size, price, dry_run, capture=None):
            fak_calls.append({"token_id": token_id, "size": float(size), "price": float(price)})
            if capture is not None:
                capture.append(
                    {
                        "status": "matched",
                        "makingAmount": str(size),
                        "takingAmount": str(round(float(size) * 0.52, 4)),
                    }
                )
            return float(size), "matched"

        ns["_fak_sell"] = priced_fak
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        cfg = _dump_cfg()
        _tick(ns, cfg, intent, cid="cid-dump")
        self.assertTrue(intent.get("sold_dump"))
        self.assertEqual(intent["sell_dump_limit"], 0.51)
        self.assertEqual(intent["sell_dump_fill_px"], 0.52)
        done = [row for row in events if row["event"] == "sell_dump_done"]
        self.assertEqual(done[0]["avg_px"], 0.52)
        clock["now"] = end + 1.0
        _tick(ns, cfg, intent, cid="cid-dump")
        risks = [row for row in events if row["event"] == "bag_risk"]
        self.assertEqual(len(risks), 1)
        self.assertAlmostEqual(risks[0]["dump_px"], 0.52)

    def test_unpriced_dump_reply_falls_back_to_the_limit(self):
        ns, events, _fak, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        cfg = _dump_cfg()
        _tick(ns, cfg, intent, cid="cid-dump")
        self.assertTrue(intent.get("sold_dump"))
        self.assertNotIn("sell_dump_fill_px", intent)
        clock["now"] = end + 1.0
        _tick(ns, cfg, intent, cid="cid-dump")
        risks = [row for row in events if row["event"] == "bag_risk"]
        self.assertAlmostEqual(risks[0]["dump_px"], 0.51)


if __name__ == "__main__":
    unittest.main()
