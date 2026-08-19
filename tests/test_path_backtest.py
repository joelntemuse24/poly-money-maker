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
    simulate_fak_buy,
    summarize,
)


def _tick(
    ts: float,
    ttm: float,
    ua: float,
    da: float,
    ub: float | None = None,
    db: float | None = None,
    uas: float | None = None,
    das: float | None = None,
):
    row = {
        "e": "tick",
        "ts": ts,
        "ttm": ttm,
        "ua": ua,
        "da": da,
        "ub": ua - 0.01 if ub is None else ub,
        "db": da - 0.01 if db is None else db,
    }
    if uas is not None:
        row["uas"] = uas
    if das is not None:
        row["das"] = das
    return row


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
        self.assertAlmostEqual(hypothetical_pnl(0.80, True, 2.5), 2.5 / 0.80 - 2.5)
        self.assertEqual(hypothetical_pnl(0.80, False, 2.5), -2.5)


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
                budget=2.5,
            )
            stats = summarize(results)
            self.assertEqual(stats["hits"], 1)
            self.assertEqual(stats["wins"], 1)
            self.assertGreater(stats["pnl_sum"], 0)
            self.assertEqual(results[0]["fill"], "full")
            self.assertEqual(stats["full"], 1)
            self.assertEqual(stats["partial"], 0)
            self.assertEqual(stats["zero"], 0)


class SimulateFakTests(unittest.TestCase):
    def test_legacy_none_size_is_full_budget(self):
        fill = simulate_fak_buy(2.5, 0.80, None)
        self.assertEqual(fill["status"], "full")
        self.assertAlmostEqual(fill["notional"], 2.5)
        self.assertAlmostEqual(fill["shares"], 2.5 / 0.80)
        self.assertAlmostEqual(fill["avg"], 0.80)

    def test_enough_size_is_full(self):
        fill = simulate_fak_buy(2.5, 0.80, 100.0)
        self.assertEqual(fill["status"], "full")
        self.assertAlmostEqual(fill["shares"], 3.125)

    def test_thin_book_partial(self):
        fill = simulate_fak_buy(15.0, 0.80, 3.0)
        self.assertEqual(fill["status"], "partial")
        self.assertAlmostEqual(fill["shares"], 3.0)
        self.assertAlmostEqual(fill["notional"], 2.4)
        self.assertAlmostEqual(fill["avg"], 0.80)

    def test_zero_size(self):
        fill = simulate_fak_buy(15.0, 0.80, 0.0)
        self.assertEqual(fill["status"], "zero")
        self.assertEqual(fill["shares"], 0.0)
        self.assertEqual(fill["notional"], 0.0)

    def test_does_not_walk_below_ask(self):
        # Only the displayed top size is available — leftover USDC is unfilled.
        fill = simulate_fak_buy(20.0, 0.80, 1.0)
        self.assertEqual(fill["status"], "partial")
        self.assertAlmostEqual(fill["shares"], 1.0)
        self.assertAlmostEqual(fill["notional"], 0.80)


class SizeAwareRuleTests(unittest.TestCase):
    def test_larger_budget_partials_same_path(self):
        ticks = [_tick(1, 90, 0.82, 0.18, uas=4.0, das=40.0)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "btc-updown-5m-2.jsonl"
            rows = [
                {
                    "e": "open",
                    "slug": "btc-updown-5m-2",
                    "series": "btc-up-or-down-5m",
                    "start": 1,
                    "end": 301,
                    "q": "test",
                },
                ticks[0],
                {"e": "resolved", "winner": "up"},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            market = load_market_file(path)
            small = evaluate_rule(
                [market],
                ask_min=0.75,
                ask_max=0.90,
                ttm_min=0,
                ttm_max=120,
                budget=2.5,
            )
            big = evaluate_rule(
                [market],
                ask_min=0.75,
                ask_max=0.90,
                ttm_min=0,
                ttm_max=120,
                budget=15.0,
            )
            self.assertEqual(small[0]["fill"], "full")
            self.assertEqual(big[0]["fill"], "partial")
            self.assertAlmostEqual(small[0]["notional"], 2.5, places=3)
            self.assertLess(big[0]["notional"], 15.0)
            self.assertAlmostEqual(big[0]["shares"], 4.0)
            self.assertAlmostEqual(big[0]["notional"], 4.0 * 0.82)

    def test_first_entry_returns_ask_size(self):
        hit = first_entry(
            [_tick(1, 60, 0.81, 0.19, uas=4.25, das=10)],
            ask_min=0.80,
            ask_max=0.90,
            ttm_min=0,
            ttm_max=120,
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit["ask_size"], 4.25)

    def test_zero_fill_not_counted_as_loss(self):
        market_rows = [
            {
                "e": "open",
                "slug": "btc-updown-5m-3",
                "series": "btc-up-or-down-5m",
                "start": 1,
                "end": 301,
                "q": "test",
            },
            _tick(1, 90, 0.81, 0.19, uas=0.0, das=0.0),
            {"e": "resolved", "winner": "up"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "btc-updown-5m-3.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in market_rows))
            market = load_market_file(path)
            results = evaluate_rule(
                [market],
                ask_min=0.75,
                ask_max=0.90,
                ttm_min=0,
                ttm_max=120,
                budget=15.0,
            )
            stats = summarize(results)
            self.assertTrue(results[0]["hit"])
            self.assertEqual(results[0]["fill"], "zero")
            self.assertIsNone(results[0]["won"])
            self.assertEqual(stats["zero"], 1)
            self.assertEqual(stats["decided"], 0)
            self.assertIsNone(stats["pnl_sum"])


class WindowAnatomyTests(unittest.TestCase):
    def test_decided_before_window(self):
        from check_path_backtest import classify_window

        ticks = [
            _tick(1, 180, 0.86, 0.14),
            _tick(2, 90, 0.88, 0.12),
            _tick(3, 10, 0.99, 0.01),
        ]
        row = classify_window(ticks, ttm_max=120, min_edge=0.05, ask_min=0.75, ask_max=0.90)
        self.assertEqual(row["bucket"], "decided_before_in_band")
        self.assertEqual(row["open_winning"], "up")

    def test_decided_before_already_above_90(self):
        from check_path_backtest import classify_window

        ticks = [
            _tick(1, 150, 0.94, 0.06),
            _tick(2, 60, 0.97, 0.03),
        ]
        row = classify_window(ticks, ttm_max=120, min_edge=0.05)
        self.assertEqual(row["bucket"], "decided_before_above_band")

    def test_tight_through_window(self):
        from check_path_backtest import classify_window

        ticks = [
            _tick(1, 180, 0.51, 0.49),
            _tick(2, 90, 0.52, 0.48),
            _tick(3, 5, 0.51, 0.49),
        ]
        row = classify_window(ticks, ttm_max=120, min_edge=0.05)
        self.assertEqual(row["bucket"], "tight_through_window")
        self.assertEqual(row["ambiguous_ticks"], 2)

    def test_cleared_in_window(self):
        from check_path_backtest import classify_window

        ticks = [
            _tick(1, 180, 0.51, 0.49),
            _tick(2, 90, 0.80, 0.20),
        ]
        row = classify_window(ticks, ttm_max=120, min_edge=0.05, ask_min=0.75, ask_max=0.90)
        self.assertEqual(row["bucket"], "cleared_in_window")
        self.assertEqual(row["first_in_band_leg"], "up")
        self.assertEqual(row["first_in_band_ttm"], 90)


if __name__ == "__main__":
    unittest.main()
