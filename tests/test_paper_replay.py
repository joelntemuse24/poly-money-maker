"""Unit tests for 92¢ 1s paper replay (no network)."""

from __future__ import annotations

import unittest

from buy.paper_replay import (
    evaluate_market,
    fak_fill,
    first_92_entry,
    quoted_buy_shares_up_to_limit,
    summarize,
    ticks_from_trades,
    walk_15m_held,
    walk_5m_held,
)


def _tick(ts, ttm, ua, da, ub=None, db=None, ult=None, dlt=None):
    return {
        "e": "tick",
        "ts": ts,
        "ttm": ttm,
        "ua": ua,
        "da": da,
        "ub": ua - 0.01 if ub is None else ub,
        "db": da - 0.01 if db is None else db,
        "ult": ua if ult is None else ult,
        "dlt": da if dlt is None else dlt,
    }


class SizerTests(unittest.TestCase):
    def test_5m_92c_posts_3_shares(self):
        shares = quoted_buy_shares_up_to_limit(2.5, 0.92, 0.92, 5.0, 3.0)
        self.assertEqual(shares, 3.0)
        fill = fak_fill("5m", 0.92)
        self.assertEqual(fill["status"], "full")
        self.assertEqual(fill["shares"], 3.0)
        self.assertAlmostEqual(fill["notional"], 2.76)

    def test_15m_pins_ask(self):
        fill = fak_fill("15m", 0.92)
        self.assertEqual(fill["status"], "full")
        self.assertGreaterEqual(fill["shares"], 2.5)
        self.assertAlmostEqual(fill["avg"], 0.92)

    def test_15m_snaps_float_92_to_tick(self):
        fill = fak_fill("15m", 0.9199999995)
        self.assertEqual(fill["status"], "full")
        self.assertAlmostEqual(fill["avg"], 0.92)


class TickRebuildTests(unittest.TestCase):
    def test_forward_fills_every_second(self):
        trades = [
            {"ts": 100, "px": 0.92, "outcome": "up", "size": 10},
            {"ts": 100, "px": 0.08, "outcome": "down", "size": 10},
            {"ts": 103, "px": 0.50, "outcome": "up", "size": 4},
            {"ts": 103, "px": 0.50, "outcome": "down", "size": 4},
        ]
        ticks = ticks_from_trades(trades, 100, 103)
        self.assertEqual(len(ticks), 4)
        self.assertAlmostEqual(ticks[0]["ua"], 0.92)
        self.assertAlmostEqual(ticks[1]["ua"], 0.92)  # forward-fill
        self.assertAlmostEqual(ticks[3]["ua"], 0.50)
        self.assertAlmostEqual(ticks[0]["ub"], 0.92)
        self.assertEqual(ticks[0]["ttm"], 3)

    def test_complement_bid_on_92_8(self):
        trades = [
            {"timestamp": 50, "price": 0.92, "outcome": "Up", "size": 1},
            {"timestamp": 50, "price": 0.08, "outcome": "Down", "size": 1},
        ]
        ticks = ticks_from_trades(trades, 50, 50)
        self.assertEqual(len(ticks), 1)
        self.assertAlmostEqual(ticks[0]["ub"], 0.92)
        self.assertAlmostEqual(ticks[0]["db"], 0.08)


class EntryTests(unittest.TestCase):
    def test_first_92_in_last_60s(self):
        ticks = [
            _tick(1, 90, 0.92, 0.08),
            _tick(2, 50, 0.92, 0.08),
            _tick(3, 40, 0.95, 0.05),
        ]
        hit = first_92_entry(ticks, ttm_max=60)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["ttm"], 50)
        self.assertEqual(hit["leg"], "up")
        self.assertAlmostEqual(hit["ask"], 0.92)

    def test_skips_wide_book(self):
        ticks = [_tick(1, 50, 0.92, 0.08, ub=0.10, db=0.01)]
        self.assertIsNone(first_92_entry(ticks, ttm_max=60))

    def test_skips_93c_float(self):
        ticks = [_tick(1, 50, 0.9299999888, 0.07)]
        self.assertIsNone(first_92_entry(ticks, ttm_max=60))


class FiveMHedgeTests(unittest.TestCase):
    def test_persist_1s_50_52_then_sell(self):
        hit = {"ts": 1000, "ttm": 90, "leg": "up", "ask": 0.92}
        fill = fak_fill("5m", 0.92)
        ticks = [_tick(1000, 90, 0.92, 0.08)]
        for i in range(3):
            ts = 1001 + i
            # Other GUI ≥ 48¢ and |held-other| ≥ 5¢ (50/52 vs 48/50 is ambiguous).
            ticks.append(
                _tick(ts, 89 - i, 0.52, 0.58, ub=0.50, db=0.56, ult=0.50, dlt=0.56)
            )
        out = walk_5m_held(ticks, hit, fill, winner="down")
        self.assertEqual(out["exit"], "hedge")
        self.assertAlmostEqual(out["exit_bid"], 0.50)
        self.assertFalse(out["hedge_late"])

    def test_dump_40_without_gui(self):
        hit = {"ts": 1, "ttm": 80, "leg": "up", "ask": 0.92}
        fill = fak_fill("5m", 0.92)
        ticks = [
            _tick(1, 80, 0.92, 0.08),
            _tick(2, 79, 0.90, 0.10, ub=0.39, db=0.01, ult=0.92, dlt=0.08),
        ]
        out = walk_5m_held(ticks, hit, fill, winner="down")
        self.assertEqual(out["exit"], "dump")
        self.assertAlmostEqual(out["exit_bid"], 0.39)

    def test_last_30s_50_90_does_not_dump(self):
        hit = {"ts": 1, "ttm": 25, "leg": "up", "ask": 0.92}
        fill = fak_fill("5m", 0.92)
        ticks = [
            _tick(1, 25, 0.92, 0.08),
            _tick(2, 24, 0.90, 0.10, ub=0.50, db=0.01, ult=0.90, dlt=0.10),
        ]
        out = walk_5m_held(ticks, hit, fill, winner="down")
        self.assertEqual(out["exit"], "redeem_loss")

    def test_last_30s_persist_58_60(self):
        hit = {"ts": 10, "ttm": 25, "leg": "up", "ask": 0.92}
        fill = fak_fill("5m", 0.92)
        ticks = [_tick(10, 25, 0.92, 0.08)]
        for i in range(3):
            ts = 11 + i
            ticks.append(
                _tick(ts, 24 - i, 0.60, 0.42, ub=0.58, db=0.40, ult=0.58, dlt=0.42)
            )
        out = walk_5m_held(ticks, hit, fill, winner="down")
        self.assertEqual(out["exit"], "hedge")
        self.assertAlmostEqual(out["exit_bid"], 0.58)
        self.assertTrue(out["hedge_late"])

    def test_one_tick_50_52_does_not_persist(self):
        """Persist needs 1s of still-qualified 50/52. A one-tick dip then 60¢ rides."""
        hit = {"ts": 1, "ttm": 90, "leg": "up", "ask": 0.92}
        fill = fak_fill("5m", 0.92)
        ticks = [
            _tick(1, 90, 0.92, 0.08),
            _tick(2, 89, 0.52, 0.50, ub=0.50, db=0.48, ult=0.50, dlt=0.50),
        ]
        for i in range(10):
            ts = 3 + i
            ticks.append(_tick(ts, 88 - i, 0.61, 0.39, ub=0.60, db=0.38, ult=0.60, dlt=0.40))
        out = walk_5m_held(ticks, hit, fill, winner="up")
        self.assertEqual(out["exit"], "redeem_win")
        self.assertAlmostEqual(out["pnl"], 3.0 - 2.76)

    def test_flatten_walk_avg_below_75(self):
        hit = {"ts": 1, "ttm": 90, "leg": "up", "ask": 0.92}
        fill = {"shares": 3.0, "notional": 2.10, "avg": 0.70, "status": "full"}
        ticks = [
            _tick(1, 90, 0.70, 0.30),
            _tick(2, 89, 0.72, 0.28, ub=0.70, db=0.27, ult=0.70, dlt=0.30),
        ]
        out = walk_5m_held(ticks, hit, fill, winner="down")
        self.assertEqual(out["exit"], "flatten")
        self.assertAlmostEqual(out["exit_bid"], 0.70)


class FifteenMHedgeTests(unittest.TestCase):
    def test_inverted_gui_blocks_35_40(self):
        hit = {"ts": 1, "ttm": 120, "leg": "up", "ask": 0.92}
        fill = fak_fill("15m", 0.92)
        ticks = [
            _tick(1, 120, 0.92, 0.08),
            _tick(2, 119, 0.40, 0.62, ub=0.35, db=0.60, ult=0.35, dlt=0.62),
        ]
        out = walk_15m_held(ticks, hit, fill, winner="down")
        # mid 37.5 > 30¢ loser cap → no hedge, redeem loss
        self.assertEqual(out["exit"], "redeem_loss")

    def test_30_70_gui_sells(self):
        hit = {"ts": 1, "ttm": 120, "leg": "up", "ask": 0.92}
        fill = fak_fill("15m", 0.92)
        ticks = [
            _tick(1, 120, 0.92, 0.08),
            _tick(2, 119, 0.30, 0.72, ub=0.28, db=0.70, ult=0.28, dlt=0.72),
        ]
        out = walk_15m_held(ticks, hit, fill, winner="down")
        self.assertEqual(out["exit"], "hedge")
        self.assertAlmostEqual(out["exit_bid"], 0.28)


class EvaluateMarketTests(unittest.TestCase):
    def test_5m_92_then_redeem_win(self):
        ticks = [_tick(10 + i, 50 - i, 0.92, 0.08) for i in range(20)]
        row = evaluate_market(ticks, series="5m", ttm_max=60, winner="up", slug="x")
        self.assertTrue(row["hit"])
        self.assertEqual(row["exit"], "redeem_win")
        self.assertAlmostEqual(row["pnl"], 3.0 - 2.76)
        stats = summarize([row])
        self.assertEqual(stats["hits"], 1)
        self.assertAlmostEqual(stats["pnl_sum"], 0.24)

    def test_15m_window_is_180s(self):
        ticks = [
            _tick(1, 200, 0.92, 0.08),
            _tick(2, 170, 0.92, 0.08),
        ]
        row = evaluate_market(ticks, series="15m", ttm_max=180, winner="up")
        self.assertTrue(row["hit"])
        self.assertEqual(row["ttm"], 170)

    def test_miss_when_never_92(self):
        ticks = [_tick(1, 50, 0.80, 0.20)]
        row = evaluate_market(ticks, series="5m", ttm_max=60, winner="up")
        self.assertFalse(row["hit"])
        self.assertIsNone(row["pnl"])


if __name__ == "__main__":
    unittest.main()
