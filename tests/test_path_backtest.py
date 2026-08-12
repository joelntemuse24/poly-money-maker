"""Unit tests for pathlog backtest helpers (no network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from check_path_backtest import (
    evaluate_rule,
    first_entry,
    hypothetical_pnl,
    infer_winner,
    load_market_file,
    summarize,
)


def _tick(ts: float, ttm: float, ua: float, da: float, ub: float | None = None, db: float | None = None):
    return {
        "e": "tick",
        "ts": ts,
        "ttm": ttm,
        "ua": ua,
        "da": da,
        "ub": ua - 0.01 if ub is None else ub,
        "db": da - 0.01 if db is None else db,
    }


class FirstEntryTests(unittest.TestCase):
    def test_picks_first_time_in_band(self):
        ticks = [
            _tick(1, 180, 0.70, 0.30),
            _tick(2, 90, 0.81, 0.19),
            _tick(3, 60, 0.88, 0.12),
        ]
        hit = first_entry(ticks, ask_min=0.80, ask_max=0.85, ttm_min=0, ttm_max=120)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["leg"], "up")
        self.assertEqual(hit["ask"], 0.81)
        self.assertEqual(hit["ttm"], 90)

    def test_skips_when_ttm_too_large(self):
        ticks = [_tick(1, 200, 0.82, 0.18)]
        hit = first_entry(ticks, ask_min=0.80, ask_max=0.90, ttm_min=0, ttm_max=120)
        self.assertIsNone(hit)

    def test_down_leg(self):
        ticks = [_tick(1, 45, 0.20, 0.81)]
        hit = first_entry(ticks, ask_min=0.80, ask_max=0.90, ttm_min=0, ttm_max=60)
        self.assertEqual(hit["leg"], "down")

    def test_spread_filter(self):
        ticks = [_tick(1, 60, 0.82, 0.18, ub=0.10, db=0.01)]
        hit = first_entry(
            ticks, ask_min=0.80, ask_max=0.90, ttm_min=0, ttm_max=120, max_spread=0.05
        )
        self.assertIsNone(hit)


class WinnerAndPnlTests(unittest.TestCase):
    def test_infer_winner_from_last_tick(self):
        ticks = [_tick(1, 10, 0.55, 0.45), _tick(2, 1, 0.99, 0.01)]
        self.assertEqual(infer_winner(ticks), "up")

    def test_pnl_win_and_loss(self):
        self.assertAlmostEqual(hypothetical_pnl(0.80, True, 3.0), 3.0 / 0.80 - 3.0)
        self.assertEqual(hypothetical_pnl(0.80, False, 3.0), -3.0)


class FileAndRuleTests(unittest.TestCase):
    def test_load_and_evaluate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "btc-updown-5m-1.jsonl"
            rows = [
                {
                    "e": "open",
                    "slug": "btc-updown-5m-1",
                    "series": "btc-up-or-down-5m",
                    "start": 1,
                    "end": 301,
                    "q": "test",
                },
                _tick(100, 90, 0.82, 0.18),
                {"e": "resolved", "winner": "up"},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            market = load_market_file(path)
            self.assertIsNotNone(market)
            self.assertEqual(market.winner, "up")
            results = evaluate_rule(
                [market],
                ask_min=0.80,
                ask_max=0.85,
                ttm_min=0,
                ttm_max=120,
                budget=3.0,
            )
            stats = summarize(results)
            self.assertEqual(stats["hits"], 1)
            self.assertEqual(stats["wins"], 1)
            self.assertGreater(stats["pnl_sum"], 0)


if __name__ == "__main__":
    unittest.main()
