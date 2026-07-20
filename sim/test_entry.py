from __future__ import annotations

import unittest

from .discovery import Market
from .entry import estimate_set_cost_from_books, simulate_fak_buy
from .shadow import new_position


class SimulateFakBuyTests(unittest.TestCase):
    def test_walks_asks_lowest_first(self):
        result = simulate_fak_buy(
            size=5,
            asks=[
                {"price": "0.52", "size": "4"},
                {"price": "0.50", "size": "2"},
                {"price": "0.51", "size": "3"},
            ],
            limit_price=0.51,
        )
        self.assertEqual(result.filled, 5)
        self.assertAlmostEqual(result.notional, 2 * 0.50 + 3 * 0.51)
        self.assertAlmostEqual(result.avg_price, 0.506)
        self.assertEqual(result.levels_used, 2)
        self.assertEqual(result.reason, "filled")

    def test_limit_produces_partial_fak_fill(self):
        result = simulate_fak_buy(
            size=5,
            asks=[
                {"price": "0.49", "size": "2"},
                {"price": "0.51", "size": "5"},
            ],
            limit_price=0.50,
        )
        self.assertEqual(result.filled, 2)
        self.assertAlmostEqual(result.avg_price, 0.49)
        self.assertEqual(result.reason, "partial")

    def test_slippage_respects_limit(self):
        result = simulate_fak_buy(
            size=3,
            asks=[{"price": "0.50", "size": "3"}],
            limit_price=0.51,
            slippage=0.01,
        )
        self.assertEqual(result.filled, 3)
        self.assertAlmostEqual(result.avg_price, 0.51)

        rejected = simulate_fak_buy(
            size=3,
            asks=[{"price": "0.505", "size": "3"}],
            limit_price=0.51,
            slippage=0.01,
        )
        self.assertEqual(rejected.filled, 0)
        self.assertEqual(rejected.reason, "no_match")

    def test_empty_and_malformed_books(self):
        result = simulate_fak_buy(
            size=5,
            asks=[{}, {"price": "bad", "size": "2"}, {"price": "0.5", "size": "0"}],
            limit_price=0.99,
        )
        self.assertEqual(result.filled, 0)
        self.assertEqual(result.reason, "empty_book")

    def test_best_ask_requires_top_depth(self):
        result = simulate_fak_buy(
            size=5,
            asks=[{"price": "0.50", "size": "4"}, {"price": "0.51", "size": "5"}],
            limit_price=0.51,
            model="best_ask",
        )
        self.assertEqual(result.filled, 0)
        self.assertEqual(result.reason, "insufficient_top_size")


class EstimateSetCostTests(unittest.TestCase):
    def test_admits_complete_pair_at_gate(self):
        estimate = estimate_set_cost_from_books(
            shares=5,
            up_asks=[{"price": "0.49", "size": "5"}],
            dn_asks=[{"price": "0.50", "size": "5"}],
            max_set_cost=0.99,
            limit_price=0.99,
        )
        self.assertTrue(estimate.complete)
        self.assertTrue(estimate.admissible)
        self.assertAlmostEqual(estimate.set_cost, 0.99)
        self.assertAlmostEqual(estimate.total_notional, 4.95)
        self.assertEqual(estimate.imbalance, 0)
        self.assertEqual(estimate.reason, "admissible")

    def test_rejects_expensive_complete_pair(self):
        estimate = estimate_set_cost_from_books(
            shares=5,
            up_asks=[{"price": "0.50", "size": "5"}],
            dn_asks=[{"price": "0.50", "size": "5"}],
            max_set_cost=0.99,
            limit_price=0.99,
        )
        self.assertTrue(estimate.complete)
        self.assertFalse(estimate.admissible)
        self.assertAlmostEqual(estimate.set_cost, 1.0)
        self.assertEqual(estimate.reason, "set_cost")

    def test_rejects_incomplete_pair_and_records_imbalance(self):
        estimate = estimate_set_cost_from_books(
            shares=5,
            up_asks=[{"price": "0.49", "size": "2"}],
            dn_asks=[{"price": "0.50", "size": "5"}],
            max_set_cost=1.10,
            limit_price=0.99,
        )
        self.assertFalse(estimate.complete)
        self.assertFalse(estimate.admissible)
        self.assertIsNone(estimate.set_cost)
        self.assertEqual(estimate.up.filled, 2)
        self.assertEqual(estimate.dn.filled, 5)
        self.assertEqual(estimate.paired_shares, 2)
        self.assertEqual(estimate.imbalance, 3)
        self.assertEqual(estimate.reason, "incomplete_up")


class ShadowEntryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.market = Market(
            condition_id="condition",
            slug="slug",
            question="question",
            end_ts=2000,
            up_token="up",
            dn_token="dn",
            series_slug="btc-up-or-down-15m",
        )
        self.sim = {"shares": 5.0, "set_cost": 1.043}

    def test_fixed_entry_position_is_unchanged(self):
        position = new_position(self.market, self.sim, 1000)
        self.assertEqual(position["set_cost"], 1.043)
        self.assertEqual(position["entry_cost_total"], 5.215)
        self.assertEqual(position["up_size"], 5.0)
        self.assertEqual(position["dn_size"], 5.0)
        self.assertNotIn("entry_model", position)
        self.assertNotIn("entry_set_cost", position)

    def test_opt_in_entry_fields_are_recorded(self):
        estimate = estimate_set_cost_from_books(
            shares=5,
            up_asks=[{"price": "0.49", "size": "5"}],
            dn_asks=[{"price": "0.50", "size": "5"}],
            max_set_cost=0.99,
            limit_price=0.99,
        )
        position = new_position(self.market, self.sim, 1000, estimate)
        self.assertEqual(position["entry_model"], "live_books")
        self.assertAlmostEqual(position["set_cost"], 0.99)
        self.assertAlmostEqual(position["entry_cost_total"], 4.95)
        self.assertEqual(position["entry_filled_up"], 5)
        self.assertEqual(position["entry_filled_dn"], 5)
        self.assertEqual(position["entry_imbalance"], 0)


if __name__ == "__main__":
    unittest.main()
