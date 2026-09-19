"""Mint loser/winner sell policy (no CLOB posts, no mintbot import)."""

from __future__ import annotations

import unittest

from buy.book import best_bid_with_min_size
from buy.mint_sell import (
    DEFAULT_SELL_KNOBS,
    classify_loser,
    inventory_latch,
    loser_ladder_limits,
    parse_sell_fill_shares,
    persist_ready,
    winner_cashout_leg,
)


class ParseSellFillSharesTests(unittest.TestCase):
    """CLOB v2 SELL: makingAmount is shares; takingAmount is USDC."""

    def test_making_amount_is_share_leg_not_usdc_taking(self):
        sold = parse_sell_fill_shares(
            {"status": "matched", "takingAmount": "1.0", "makingAmount": "50.0"},
            offered_shares=50.0,
        )
        self.assertEqual(sold, 50.0)

    def test_taking_amount_alone_is_not_treated_as_shares(self):
        sold = parse_sell_fill_shares(
            {"status": "matched", "takingAmount": "1.0"},
            offered_shares=50.0,
        )
        self.assertEqual(sold, 0.0)

    def test_micro_unit_making_amount(self):
        sold = parse_sell_fill_shares(
            {
                "status": "matched",
                "takingAmount": "1000000",
                "makingAmount": "50000000",
            },
            offered_shares=50.0,
        )
        self.assertEqual(sold, 50.0)

    def test_size_matched_preferred(self):
        sold = parse_sell_fill_shares(
            {
                "status": "matched",
                "size_matched": "5.0",
                "takingAmount": "0.10",
                "makingAmount": "5.0",
            },
            offered_shares=5.0,
        )
        self.assertEqual(sold, 5.0)

    def test_empty_and_invalid_are_zero(self):
        self.assertEqual(parse_sell_fill_shares({}, 10.0), 0.0)
        self.assertEqual(parse_sell_fill_shares(None, 10.0), 0.0)
        self.assertEqual(
            parse_sell_fill_shares(
                {"makingAmount": "999", "status": "matched"},
                offered_shares=5.0,
            ),
            0.0,
        )


class InventoryLatchTests(unittest.TestCase):
    def test_transient_zero_does_not_latch_before_inventory(self):
        self.assertEqual(
            inventory_latch(0.0, tol=0.01, seen_inventory=False),
            "await_inventory",
        )

    def test_zero_after_seen_inventory_is_already_flat(self):
        self.assertEqual(
            inventory_latch(0.0, tol=0.01, seen_inventory=True),
            "already_flat",
        )

    def test_positive_balance_is_inventory(self):
        self.assertEqual(
            inventory_latch(5.0, tol=0.01, seen_inventory=False),
            "has_inventory",
        )

    def test_unknown_balance_does_not_latch(self):
        self.assertEqual(
            inventory_latch(None, tol=0.01, seen_inventory=False),
            "unknown",
        )


class LoserArmTests(unittest.TestCase):
    def test_loser_requires_opposite_near_ninety(self):
        leg, reason = classify_loser(
            up_bid=0.03, dn_bid=0.90, threshold=0.03, opposite_min=0.90,
        )
        self.assertEqual(leg, "up")
        self.assertEqual(reason, "loser")

        leg, reason = classify_loser(
            up_bid=0.03, dn_bid=0.50, threshold=0.03, opposite_min=0.90,
        )
        self.assertIsNone(leg)
        self.assertEqual(reason, "wick_unconfirmed")

    def test_both_cheap_and_neither(self):
        leg, reason = classify_loser(
            up_bid=0.02, dn_bid=0.03, threshold=0.03, opposite_min=0.90,
        )
        self.assertIsNone(leg)
        self.assertEqual(reason, "both_cheap")

        leg, reason = classify_loser(
            up_bid=0.40, dn_bid=0.60, threshold=0.03, opposite_min=0.90,
        )
        self.assertIsNone(leg)
        self.assertEqual(reason, "none")

    def test_down_loser_when_up_is_winner(self):
        leg, reason = classify_loser(
            up_bid=0.92, dn_bid=0.025, threshold=0.03, opposite_min=0.90,
        )
        self.assertEqual(leg, "dn")
        self.assertEqual(reason, "loser")


class PersistReadyTests(unittest.TestCase):
    def test_arms_then_waits_then_fires(self):
        fire, armed, why = persist_ready(
            True, now_s=10.0, armed_ts=None, persist_s=5.0,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "armed")

        fire, armed, why = persist_ready(
            True, now_s=14.9, armed_ts=10.0, persist_s=5.0,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "waiting")

        fire, armed, why = persist_ready(
            True, now_s=15.0, armed_ts=10.0, persist_s=5.0,
        )
        self.assertTrue(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "ready")

    def test_reset_when_qualify_drops(self):
        fire, armed, why = persist_ready(
            False, now_s=12.0, armed_ts=10.0, persist_s=5.0,
        )
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")

    def test_immediate_when_persist_off(self):
        fire, _armed, why = persist_ready(
            True, now_s=10.0, armed_ts=None, persist_s=0.0,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "immediate")


class WinnerCashoutTests(unittest.TestCase):
    def test_default_winner_min_matches_099_book_top(self):
        self.assertAlmostEqual(DEFAULT_SELL_KNOBS["sell_winner_min"], 0.99)

    def test_winner_at_999(self):
        self.assertEqual(
            winner_cashout_leg(up_bid=0.999, dn_bid=0.001, winner_min=0.999),
            "up",
        )
        self.assertIsNone(
            winner_cashout_leg(up_bid=0.90, dn_bid=0.10, winner_min=0.999),
        )

    def test_winner_arms_at_99_when_min_is_99_not_999(self):
        """Polymarket 15m books top at 0.99; 0.999 never prints."""
        self.assertEqual(
            winner_cashout_leg(up_bid=0.01, dn_bid=0.99, winner_min=0.99),
            "dn",
        )
        self.assertIsNone(
            winner_cashout_leg(up_bid=0.01, dn_bid=0.99, winner_min=0.999),
        )

    def test_both_at_999_is_skipped(self):
        self.assertIsNone(
            winner_cashout_leg(up_bid=0.999, dn_bid=0.999, winner_min=0.999),
        )


class LoserLadderTests(unittest.TestCase):
    def test_persist_then_threshold_then_floor(self):
        self.assertEqual(
            loser_ladder_limits(threshold=0.03, floor=0.02, loser_bid=0.03),
            [0.03, 0.02],
        )

    def test_live_bid_between_threshold_and_floor(self):
        self.assertEqual(
            loser_ladder_limits(threshold=0.03, floor=0.02, loser_bid=0.025),
            [0.025, 0.02],
        )


class SizedBidTests(unittest.TestCase):
    def test_skips_toxic_thin_top_bid(self):
        price, size = best_bid_with_min_size(
            [
                {"price": "0.03", "size": "0.01"},
                {"price": "0.029", "size": "50"},
            ],
            min_size=1.0,
        )
        self.assertEqual(price, 0.029)
        self.assertEqual(size, 50.0)

    def test_zero_size_top_is_ignored(self):
        price, size = best_bid_with_min_size(
            [
                {"price": 0.90, "size": 0},
                {"price": 0.89, "size": 12},
            ],
            min_size=1.0,
        )
        self.assertEqual(price, 0.89)
        self.assertEqual(size, 12.0)


if __name__ == "__main__":
    unittest.main()
