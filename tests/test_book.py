"""Unit tests for shared CLOB top-of-book parsing (no network)."""

from __future__ import annotations

import unittest

from buy.book import best_bid_with_min_size, best_from_levels, bid_fill_depth


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


class BestBidWithMinSizeTests(unittest.TestCase):
    def test_requires_displayed_size(self):
        price, size = best_bid_with_min_size(
            [
                {"price": "0.03", "size": "0.2"},
                {"price": "0.028", "size": "8"},
            ],
            min_size=1.0,
        )
        self.assertEqual(price, 0.028)
        self.assertEqual(size, 8.0)

    def test_none_when_all_thin(self):
        self.assertEqual(
            best_bid_with_min_size(
                [{"price": 0.99, "size": 0.1}],
                min_size=1.0,
            ),
            (None, 0.0),
        )


class BidFillDepthTests(unittest.TestCase):
    """Cumulative bid size a sell FAK at `limit` can take (price >= limit)."""

    _TOY = [
        {"price": "0.02", "size": "5"},
        {"price": "0.01", "size": "20"},
    ]

    def test_toy_book_depth_at_2c_and_1c(self):
        at_2c = bid_fill_depth(self._TOY, 0.02)
        self.assertEqual(at_2c["best_bid"], 0.02)
        self.assertEqual(at_2c["best_bid_size"], 5.0)
        self.assertEqual(at_2c["depth_at_limit"], 5.0)
        self.assertEqual(
            at_2c["ladder"],
            [
                {"price": 0.02, "depth": 5.0},
                {"price": 0.01, "depth": 25.0},
            ],
        )

        at_1c = bid_fill_depth(self._TOY, 0.01)
        self.assertEqual(at_1c["best_bid"], 0.02)
        self.assertEqual(at_1c["best_bid_size"], 5.0)
        self.assertEqual(at_1c["depth_at_limit"], 25.0)
        self.assertEqual(at_1c["ladder"], [{"price": 0.01, "depth": 25.0}])

    def test_fak_above_book_is_zero_then_walks_down(self):
        snap = bid_fill_depth(self._TOY, 0.03)
        self.assertEqual(snap["depth_at_limit"], 0.0)
        self.assertEqual(
            snap["ladder"],
            [
                {"price": 0.03, "depth": 0.0},
                {"price": 0.02, "depth": 5.0},
                {"price": 0.01, "depth": 25.0},
            ],
        )

    def test_empty_and_junk(self):
        empty = bid_fill_depth([], 0.02)
        self.assertIsNone(empty["best_bid"])
        self.assertEqual(empty["best_bid_size"], 0.0)
        self.assertEqual(empty["depth_at_limit"], 0.0)
        self.assertEqual(
            empty["ladder"],
            [
                {"price": 0.02, "depth": 0.0},
                {"price": 0.01, "depth": 0.0},
            ],
        )
        self.assertEqual(bid_fill_depth(None, 0.02)["depth_at_limit"], 0.0)

    def test_merges_duplicate_prices(self):
        snap = bid_fill_depth(
            [
                {"price": 0.02, "size": 3},
                {"price": "0.02", "size": "2"},
                {"price": 0.01, "size": 4},
            ],
            0.02,
        )
        self.assertEqual(snap["best_bid_size"], 5.0)
        self.assertEqual(snap["depth_at_limit"], 5.0)


if __name__ == "__main__":
    unittest.main()

