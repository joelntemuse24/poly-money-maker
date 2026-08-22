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
    matches_series,
    paper_settle,
    simulate_fak_buy,
    summarize,
    sweep_variants,
    template_from_strategy,
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


class SeriesFilterTests(unittest.TestCase):
    def test_5m_does_not_match_15m(self):
        self.assertTrue(matches_series("btc-up-or-down-5m", "btc-updown-5m-1", "5m"))
        self.assertFalse(matches_series("btc-up-or-down-15m", "btc-updown-15m-1", "5m"))
        self.assertTrue(matches_series("btc-up-or-down-15m", "btc-updown-15m-1", "15m"))
        self.assertFalse(matches_series("btc-up-or-down-5m", "btc-updown-5m-1", "15m"))
        self.assertTrue(matches_series("btc-up-or-down-hourly", "btc-updown-hourly-1", "hourly"))
        self.assertFalse(matches_series("btc-up-or-down-5m", "btc-updown-5m-1", "hourly"))


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
        row = classify_window(
            ticks, ttm_max=120, min_edge=0.05, ask_min=0.75, ask_max=0.90,
        )
        self.assertEqual(row["bucket"], "decided_before_above_band")
        live = classify_window(ticks, ttm_max=120, min_edge=0.05)
        self.assertEqual(live["bucket"], "decided_before_in_band")

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


class PaperExitTests(unittest.TestCase):
    def test_gui_proxy_hedges_tight_reversal(self):
        fill = simulate_fak_buy(2.5, 0.80, None)
        hit = {"ts": 1, "ttm": 90, "leg": "up", "ask": 0.80}
        ticks = [
            _tick(1, 90, 0.80, 0.20),
            _tick(2, 40, 0.32, 0.72, ub=0.25, db=0.68),
        ]
        out = paper_settle(ticks, hit, fill, winner="down")
        self.assertEqual(out["exit"], "hedge")
        self.assertAlmostEqual(out["exit_bid"], 0.25)
        self.assertLess(out["pnl"], 0)
        self.assertGreater(out["pnl"], -2.5)

    def test_wide_book_does_not_hedge_without_last_trade(self):
        fill = simulate_fak_buy(2.5, 0.80, None)
        hit = {"ts": 1, "ttm": 90, "leg": "up", "ask": 0.80}
        ticks = [
            _tick(1, 90, 0.80, 0.20),
            _tick(2, 40, 0.32, 0.99, ub=0.01, db=0.01),
        ]
        out = paper_settle(ticks, hit, fill, winner="down")
        self.assertEqual(out["exit"], "redeem_loss")
        self.assertAlmostEqual(out["pnl"], -2.5)

    def test_toxic_recovered_book_rides(self):
        fill = simulate_fak_buy(2.5, 0.60, None)
        hit = {"ts": 1, "ttm": 90, "leg": "up", "ask": 0.60}
        ticks = [
            _tick(1, 90, 0.60, 0.40),
            _tick(2, 50, 0.97, 0.03, ub=0.97, db=0.02),
        ]
        out = paper_settle(ticks, hit, fill, winner="up", toxic_force_exit_below=0.65)
        self.assertEqual(out["exit"], "redeem_win")
        self.assertGreater(out["pnl"], 0)

    def test_toxic_dead_book_dumps_without_gui(self):
        fill = simulate_fak_buy(2.5, 0.60, None)
        hit = {"ts": 1, "ttm": 90, "leg": "up", "ask": 0.60}
        ticks = [
            _tick(1, 90, 0.60, 0.40),
            _tick(2, 50, 0.99, 0.11, ub=0.11, db=0.01),
        ]
        out = paper_settle(ticks, hit, fill, winner="up", toxic_force_exit_below=0.65)
        self.assertEqual(out["exit"], "toxic_dump")
        self.assertAlmostEqual(out["exit_bid"], 0.11)

    def test_5m_paper_hedges_53c_stop_while_held_still_ahead(self):
        fill = simulate_fak_buy(2.5, 0.90, None)
        hit = {"ts": 1, "ttm": 90, "leg": "up", "ask": 0.90}
        ticks = [
            _tick(1, 90, 0.90, 0.10),
            _tick(2, 40, 0.54, 0.48, ub=0.52, db=0.46),
        ]
        out = paper_settle(
            ticks, hit, fill, winner="down",
            hedge_threshold=0.53,
            hedge_require_ask_max=0.55,
            last_trade_max=0.55,
            hedge_held_gui_max=0.55,
            hedge_other_gui_min=0.45,
            require_gui_reversed=False,
        )
        self.assertEqual(out["exit"], "hedge")
        self.assertAlmostEqual(out["exit_bid"], 0.52)

    def test_toxic_60c_does_not_dump_when_toxic_floor_is_53(self):
        fill = simulate_fak_buy(2.5, 0.60, None)
        hit = {"ts": 1, "ttm": 90, "leg": "up", "ask": 0.60}
        ticks = [
            _tick(1, 90, 0.60, 0.40),
            _tick(2, 50, 0.99, 0.40, ub=0.60, db=0.01),
        ]
        out = paper_settle(
            ticks, hit, fill, winner="up",
            toxic_force_exit_below=0.65,
            hedge_threshold=0.70,
            hedge_toxic_bid_max=0.53,
        )
        self.assertEqual(out["exit"], "redeem_win")


class SweepTemplateTests(unittest.TestCase):
    def test_template_reads_5m_example(self):
        tmpl = template_from_strategy(
            Path(__file__).resolve().parents[1] / "strategy_buy5m.example.json"
        )
        self.assertEqual(tmpl["ask_min"], 0.75)
        self.assertEqual(tmpl["ask_max"], 0.90)
        self.assertEqual(tmpl["ttm_max"], 120.0)
        self.assertEqual(tmpl["budget"], 2.5)
        self.assertTrue(tmpl["hedge_require_gui"])
        self.assertEqual(tmpl["hedge_threshold"], 0.70)
        self.assertEqual(tmpl["hedge_require_ask_max"], 0.72)
        self.assertEqual(tmpl["hedge_toxic_bid_max"], 0.53)
        self.assertEqual(tmpl["hedge_held_gui_max"], 0.72)
        self.assertEqual(tmpl["hedge_other_gui_min"], 0.28)
        self.assertFalse(tmpl["require_gui_reversed"])

    def test_template_15m_example_stays_buy_max_price(self):
        tmpl = template_from_strategy(
            Path(__file__).resolve().parents[1] / "strategy_buy.example.json"
        )
        self.assertEqual(tmpl["ask_max"], 0.90)

    def test_sweep_starts_from_live_template(self):
        tmpl = template_from_strategy(
            Path(__file__).resolve().parents[1] / "strategy_buy5m.example.json"
        )
        names = [row["name"] for row in sweep_variants(tmpl)]
        self.assertEqual(names[0], "live_5m_paper")
        self.assertEqual(sweep_variants(tmpl)[0]["ask_max"], 0.90)
        self.assertIn("live_5m_ride", names)
        self.assertIn("window_180s", names)
        self.assertIn("band_70_99", names)
        self.assertIn("band_75_90", names)
        self.assertIn("budget_15", names)
        self.assertIn("no_spread_cap", names)

    def test_sweep_without_ticks_exits_2(self):
        from check_path_backtest import main

        with tempfile.TemporaryDirectory() as tmp:
            rc = main(["--sweep", "--series", "5m", "--dir", tmp])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()

