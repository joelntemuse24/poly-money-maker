"""Unit tests for shared CLOB top-of-book parsing (no network)."""

from __future__ import annotations

import unittest

from buy.book import best_from_levels


class BestFromLevelsTests(unittest.TestCase):
    def test_best_ask_is_lowest_price_with_size(self):
        price, size = best_from_levels(
            [
                {"price": "0.84", "size": "10"},
                {"price": "0.81", "size": "3.5"},
                {"price": "0.90", "size": "100"},
            ],
            "ask",
        )
        self.assertEqual(price, 0.81)
        self.assertEqual(size, 3.5)

    def test_best_bid_is_highest_price_with_size(self):
        price, size = best_from_levels(
            [{"price": 0.79, "size": 2}, {"price": 0.80, "size": 8}],
            "bid",
        )
        self.assertEqual(price, 0.80)
        self.assertEqual(size, 8)

    def test_empty_and_junk(self):
        self.assertEqual(best_from_levels([], "ask"), (None, 0.0))
        self.assertEqual(best_from_levels(None, "bid"), (None, 0.0))
        self.assertEqual(
            best_from_levels([{"price": 0.5, "size": 0}, {"price": 1.2, "size": 5}], "ask"),
            (None, 0.0),
        )

    def test_skips_non_dict_levels(self):
        price, size = best_from_levels(
            ["0.8", {"price": "0.82", "size": "1.25"}],
            "ask",
        )
        self.assertEqual(price, 0.82)
        self.assertEqual(size, 1.25)


if __name__ == "__main__":
    unittest.main()
