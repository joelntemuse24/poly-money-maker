"""Mint loser/winner sell policy (no CLOB posts, no mintbot import)."""

from __future__ import annotations

import unittest

from buy.book import best_bid_with_min_size
from buy.mint_sell import (
    classify_loser,
    cycle_sleep_s,
    effective_loser_persist_s,
    empty_fak_status,
    inventory_latch,
    loser_empty_keep_qualify,
    loser_ladder_limits,
    loser_persist_ready,
    parse_sell_fill_shares,
    persist_ready,
    sell_intent_hot,
    sell_window_open,
    skip_mint_discovery_for_sell,
    winner_cashout_leg,
    winner_cheap_decision,
    winner_sell_limit,
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


class EffectiveLoserPersistTests(unittest.TestCase):
    """Last 60s before end_ts uses 5s persist so a short qualify window can fire."""

    _KNOBS = dict(persist_s=9.0, last_min_s=5.0, last_min_window_s=60.0)

    def test_ttm_over_60_uses_normal_9s(self):
        end = 10_000.0
        self.assertEqual(
            effective_loser_persist_s(now_s=end - 61.0, end_ts=end, **self._KNOBS),
            9.0,
        )
        self.assertEqual(
            effective_loser_persist_s(now_s=end - 90.0, end_ts=end, **self._KNOBS),
            9.0,
        )

    def test_ttm_30_uses_last_min_5s(self):
        end = 10_000.0
        self.assertEqual(
            effective_loser_persist_s(now_s=end - 30.0, end_ts=end, **self._KNOBS),
            5.0,
        )

    def test_ttm_exactly_60_uses_last_min(self):
        end = 10_000.0
        self.assertEqual(
            effective_loser_persist_s(now_s=end - 60.0, end_ts=end, **self._KNOBS),
            5.0,
        )

    def test_ttm_zero_or_past_end_is_no_sell(self):
        end = 10_000.0
        self.assertIsNone(
            effective_loser_persist_s(now_s=end, end_ts=end, **self._KNOBS)
        )
        self.assertIsNone(
            effective_loser_persist_s(now_s=end + 0.01, end_ts=end, **self._KNOBS)
        )
        self.assertFalse(sell_window_open(now_s=end, end_ts=end))
        self.assertFalse(sell_window_open(now_s=end + 1.0, end_ts=end))
        self.assertTrue(sell_window_open(now_s=end - 0.01, end_ts=end))

    def test_arm_across_last_min_boundary_uses_shorter_threshold_without_reset(self):
        """Incident: 9s arm started before T-60; last minute re-evaluates at 5s.

        Arm at T-63 (9s). At T-58 elapsed is 5s: still waiting under 9s, ready
        under 5s, and armed_ts is unchanged (do not reset on the switch).
        """
        end = 10_000.0
        arm_now = end - 63.0
        tick_now = end - 58.0
        persist_at_arm = effective_loser_persist_s(
            now_s=arm_now, end_ts=end, **self._KNOBS
        )
        self.assertEqual(persist_at_arm, 9.0)
        fire, armed, why = persist_ready(
            True, now_s=arm_now, armed_ts=None, persist_s=persist_at_arm,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, arm_now)
        self.assertEqual(why, "armed")

        persist_inside = effective_loser_persist_s(
            now_s=tick_now, end_ts=end, **self._KNOBS
        )
        self.assertEqual(persist_inside, 5.0)
        still_waiting, armed_9, why_9 = persist_ready(
            True, now_s=tick_now, armed_ts=armed, persist_s=9.0,
        )
        self.assertFalse(still_waiting)
        self.assertEqual(armed_9, armed)
        self.assertEqual(why_9, "waiting")

        fire, armed_5, why_5 = persist_ready(
            True, now_s=tick_now, armed_ts=armed, persist_s=persist_inside,
        )
        self.assertTrue(fire)
        self.assertEqual(armed_5, armed)
        self.assertEqual(why_5, "ready")

        fire_l, armed_l, why_l = loser_persist_ready(
            True, now_s=tick_now, armed_ts=armed, persist_s=persist_inside,
        )
        self.assertTrue(fire_l)
        self.assertEqual(armed_l, armed)
        self.assertEqual(why_l, "ready")


class WinnerCashoutTests(unittest.TestCase):
    def test_winner_at_999(self):
        self.assertEqual(
            winner_cashout_leg(up_bid=0.999, dn_bid=0.001, winner_min=0.999),
            "up",
        )
        self.assertIsNone(
            winner_cashout_leg(up_bid=0.90, dn_bid=0.10, winner_min=0.999),
        )

    def test_both_at_999_is_skipped(self):
        self.assertIsNone(
            winner_cashout_leg(up_bid=0.999, dn_bid=0.999, winner_min=0.999),
        )


class WinnerCheapDecisionTests(unittest.TestCase):
    """Incident: 1¢ loser + 99¢ winner is flat vs mint; prefer redeem."""

    _KNOBS = dict(winner_min=0.999, cheap_gate=0.03, cheap_min=0.99)

    def test_loser_1c_plus_cheap_99c_does_not_enable_cheap(self):
        effective, cheap, reason = winner_cheap_decision(
            sold_loser=True, loser_fill=0.01, **self._KNOBS
        )
        self.assertEqual(effective, 0.999)
        self.assertFalse(cheap)
        self.assertEqual(reason, "flat_or_negative_edge")
        # CLOB max is 0.99; holding 0.999 means the winner cannot fill.
        self.assertIsNone(
            winner_cashout_leg(up_bid=0.01, dn_bid=0.99, winner_min=effective)
        )

    def test_loser_2c_plus_cheap_99c_enables_cheap(self):
        effective, cheap, reason = winner_cheap_decision(
            sold_loser=True, loser_fill=0.02, **self._KNOBS
        )
        self.assertEqual(effective, 0.99)
        self.assertTrue(cheap)
        self.assertEqual(reason, "positive_edge")
        self.assertEqual(
            winner_cashout_leg(up_bid=0.02, dn_bid=0.99, winner_min=effective),
            "dn",
        )

    def test_loser_3c_plus_cheap_99c_enables_cheap(self):
        effective, cheap, reason = winner_cheap_decision(
            sold_loser=True, loser_fill=0.03, **self._KNOBS
        )
        self.assertEqual(effective, 0.99)
        self.assertTrue(cheap)
        self.assertEqual(reason, "positive_edge")

    def test_without_sold_loser_no_cheap(self):
        effective, cheap, reason = winner_cheap_decision(
            sold_loser=False, loser_fill=0.02, **self._KNOBS
        )
        self.assertEqual(effective, 0.999)
        self.assertFalse(cheap)
        self.assertEqual(reason, "no_sold_loser")


class WinnerSellLimitTests(unittest.TestCase):
    """Incident 1789810200: live-bid FAK at 0.995–0.999 is rejected (CLOB max 0.99)."""

    def test_live_999_posts_clob_max(self):
        posted, clamped, reason = winner_sell_limit(0.999)
        self.assertEqual(posted, 0.99)
        self.assertTrue(clamped)
        self.assertEqual(reason, "clob_max")

    def test_live_995_posts_clob_max(self):
        posted, clamped, reason = winner_sell_limit(0.995)
        self.assertEqual(posted, 0.99)
        self.assertTrue(clamped)
        self.assertEqual(reason, "clob_max")

    def test_live_99_is_already_valid(self):
        posted, clamped, reason = winner_sell_limit(0.99)
        self.assertEqual(posted, 0.99)
        self.assertFalse(clamped)
        self.assertEqual(reason, "")

    def test_live_below_tick_floors_to_clob_min(self):
        posted, clamped, reason = winner_sell_limit(0.005)
        self.assertEqual(posted, 0.01)
        self.assertTrue(clamped)
        self.assertEqual(reason, "clob_min")

    def test_rich_path_999_bid_still_posts_99(self):
        """Joel: take winner when sized bid ≥ 0.999, but FAK must be a valid CLOB price."""
        self.assertEqual(
            winner_cashout_leg(up_bid=0.001, dn_bid=0.999, winner_min=0.999),
            "dn",
        )
        posted, clamped, reason = winner_sell_limit(0.999)
        self.assertEqual(posted, 0.99)
        self.assertTrue(clamped)
        self.assertEqual(reason, "clob_max")

    def test_cheap_gate_995_book_still_posts_99(self):
        """Incident: loser @0.03 opened cheap (1.02); Down 0.995 must FAK 0.99 not 0.995."""
        effective, cheap, why = winner_cheap_decision(
            sold_loser=True,
            loser_fill=0.03,
            winner_min=0.999,
            cheap_gate=0.03,
            cheap_min=0.99,
        )
        self.assertEqual(effective, 0.99)
        self.assertTrue(cheap)
        self.assertEqual(why, "positive_edge")
        self.assertEqual(
            winner_cashout_leg(up_bid=0.03, dn_bid=0.995, winner_min=effective),
            "dn",
        )
        posted, clamped, reason = winner_sell_limit(0.995)
        self.assertEqual(posted, 0.99)
        self.assertTrue(clamped)
        self.assertEqual(reason, "clob_max")

    def test_live_98_with_min_99_does_not_fire(self):
        self.assertIsNone(
            winner_cashout_leg(up_bid=0.02, dn_bid=0.98, winner_min=0.99)
        )

    def test_cheap_gate_still_requires_loser_plus_99_beats_one(self):
        effective, cheap, reason = winner_cheap_decision(
            sold_loser=True,
            loser_fill=0.01,
            winner_min=0.999,
            cheap_gate=0.03,
            cheap_min=0.99,
        )
        self.assertEqual(effective, 0.999)
        self.assertFalse(cheap)
        self.assertEqual(reason, "flat_or_negative_edge")
        self.assertIsNone(
            winner_cashout_leg(up_bid=0.01, dn_bid=0.99, winner_min=effective)
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

    def test_bid_below_floor_limit_equals_live_bid(self):
        """Incident 1789800300: 1¢ book must FAK at 1¢, not clamp to the 2¢ floor."""
        self.assertEqual(
            loser_ladder_limits(threshold=0.03, floor=0.02, loser_bid=0.01),
            [0.01],
        )
        self.assertEqual(
            loser_ladder_limits(threshold=0.03, floor=0.02, loser_bid=0.015),
            [0.015],
        )

    def test_floor_and_above_still_use_threshold_then_floor(self):
        self.assertEqual(
            loser_ladder_limits(threshold=0.03, floor=0.02, loser_bid=0.02),
            [0.02],
        )
        self.assertEqual(
            loser_ladder_limits(threshold=0.03, floor=0.02, loser_bid=0.04),
            [0.03, 0.02],
        )


class EmptyFakArmTests(unittest.TestCase):
    def test_empty_fak_status_matches_clob_miss(self):
        self.assertTrue(
            empty_fak_status("error:no orders found to match with FAK order")
        )
        self.assertTrue(
            empty_fak_status(
                "error:PolyApiException[status_code=400, error_message="
                "{'error': 'no orders found to match with FAK order'}]"
            )
        )
        self.assertFalse(empty_fak_status("matched"))
        self.assertFalse(empty_fak_status("error:timeout"))

    def test_empty_fak_does_not_clear_arm_forever(self):
        fire, armed, why = loser_persist_ready(
            False,
            now_s=20.0,
            armed_ts=10.0,
            persist_s=5.0,
            last_status="error:no orders found to match with FAK order",
            book_empty=True,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "empty_fak_keep_arm")

        fire, armed, why = loser_persist_ready(
            True,
            now_s=21.0,
            armed_ts=armed,
            persist_s=5.0,
            last_status="error:no orders found to match with FAK order",
            book_empty=False,
        )
        self.assertTrue(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "ready")

    def test_empty_fak_rearms_when_latch_already_gone(self):
        fire, armed, why = loser_persist_ready(
            False,
            now_s=20.0,
            armed_ts=None,
            persist_s=5.0,
            last_status="error:no orders found to match with FAK order",
            book_empty=True,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 20.0)
        self.assertEqual(why, "empty_fak_rearm")

    def test_qualify_drop_with_visible_book_still_resets(self):
        fire, armed, why = loser_persist_ready(
            False,
            now_s=20.0,
            armed_ts=10.0,
            persist_s=5.0,
            last_status="matched",
            book_empty=False,
        )
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")


class EmptyLoserBookKeepArmTests(unittest.TestCase):
    """Late empty loser book must not wipe persist; 3→2→1 stays continuous."""

    _THR = 0.03
    _OPP = 0.90
    _PERSIST = 9.0

    def _tick(self, up_bid, dn_bid, now_s, armed_ts, prev_leg=None, last_status=None):
        loser, _reason = classify_loser(
            up_bid, dn_bid, threshold=self._THR, opposite_min=self._OPP,
        )
        keep, keep_leg = loser_empty_keep_qualify(
            armed_ts=armed_ts,
            up_bid=up_bid,
            dn_bid=dn_bid,
            opposite_min=self._OPP,
            prev_leg=prev_leg or loser,
        )
        fire, armed, why = loser_persist_ready(
            loser is not None,
            now_s=now_s,
            armed_ts=armed_ts,
            persist_s=self._PERSIST,
            last_status=last_status,
            book_empty=keep,
        )
        if why == "reset":
            leg = None
        else:
            leg = loser or keep_leg or prev_leg
        return fire, armed, why, leg

    def test_empty_after_arm_keeps_clock_then_fires_on_return(self):
        """Arm at 3¢, bid None for 3s, then 2¢: armed_ts unchanged; fire if elapsed ≥ persist_s."""
        fire, armed, why, leg = self._tick(0.03, 0.90, 10.0, None)
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "armed")
        self.assertEqual(leg, "up")

        fire, armed, why, leg = self._tick(
            None, 0.90, 13.0, armed, prev_leg=leg, last_status=None,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "empty_keep_arm")
        self.assertEqual(leg, "up")

        fire, armed, why, _leg = self._tick(0.02, 0.90, 19.0, armed, prev_leg=leg)
        self.assertTrue(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "ready")

    def test_empty_past_persist_fires_immediately_when_bid_returns(self):
        fire, armed, why, leg = self._tick(0.03, 0.91, 10.0, None)
        self.assertEqual(why, "armed")
        fire, armed, why, leg = self._tick(None, 0.91, 20.0, armed, prev_leg=leg)
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "empty_keep_arm")
        fire, armed, why, _leg = self._tick(0.01, 0.91, 20.1, armed, prev_leg=leg)
        self.assertTrue(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "ready")

    def test_bid_above_threshold_resets(self):
        fire, armed, why, leg = self._tick(0.03, 0.90, 10.0, None)
        self.assertEqual(armed, 10.0)
        fire, armed, why, leg = self._tick(0.04, 0.90, 12.0, armed, prev_leg=leg)
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")
        self.assertIsNone(leg)

    def test_never_armed_empty_does_not_keep(self):
        keep, keep_leg = loser_empty_keep_qualify(
            armed_ts=None, up_bid=None, dn_bid=0.92, opposite_min=self._OPP,
        )
        self.assertFalse(keep)
        self.assertIsNone(keep_leg)
        fire, armed, why = loser_persist_ready(
            False, now_s=10.0, armed_ts=None, persist_s=self._PERSIST, book_empty=False,
        )
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")
        fire, armed, why, leg = self._tick(None, 0.92, 10.0, None)
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")
        self.assertIsNone(leg)

    def test_step_3_2_1_is_continuous(self):
        fire, armed, why, leg = self._tick(0.03, 0.90, 10.0, None)
        self.assertEqual(why, "armed")
        self.assertEqual(armed, 10.0)
        fire, armed, why, leg = self._tick(0.02, 0.90, 13.0, armed, prev_leg=leg)
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "waiting")
        fire, armed, why, leg = self._tick(0.01, 0.90, 16.0, armed, prev_leg=leg)
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "waiting")
        fire, armed, why, _leg = self._tick(0.01, 0.90, 19.0, armed, prev_leg=leg)
        self.assertTrue(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "ready")

    def test_opposite_below_min_resets_even_if_loser_empty(self):
        fire, armed, why, leg = self._tick(0.03, 0.90, 10.0, None)
        fire, armed, why, _leg = self._tick(None, 0.50, 12.0, armed, prev_leg=leg)
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")

    def test_both_cheap_resets(self):
        fire, armed, why, leg = self._tick(0.03, 0.90, 10.0, None)
        fire, armed, why, _leg = self._tick(0.02, 0.03, 12.0, armed, prev_leg=leg)
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")

    def test_plain_empty_keep_does_not_require_fak_status(self):
        fire, armed, why = loser_persist_ready(
            False,
            now_s=13.0,
            armed_ts=10.0,
            persist_s=9.0,
            last_status=None,
            book_empty=True,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "empty_keep_arm")

    def test_down_loser_empty_keep_and_both_books_empty(self):
        fire, armed, why, leg = self._tick(0.91, 0.03, 10.0, None)
        self.assertEqual(leg, "dn")
        fire, armed, why, leg = self._tick(0.91, None, 13.0, armed, prev_leg=leg)
        self.assertEqual(why, "empty_keep_arm")
        self.assertEqual(armed, 10.0)
        self.assertEqual(leg, "dn")
        fire, armed, why, leg = self._tick(None, None, 16.0, armed, prev_leg=leg)
        self.assertEqual(why, "empty_keep_arm")
        self.assertEqual(armed, 10.0)
        fire, armed, why, _leg = self._tick(0.91, 0.01, 19.0, armed, prev_leg=leg)
        self.assertTrue(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "ready")

    def test_sold_loser_does_not_keep_empty_arm(self):
        keep, _leg = loser_empty_keep_qualify(
            armed_ts=10.0,
            up_bid=None,
            dn_bid=0.92,
            opposite_min=self._OPP,
            prev_leg="up",
            sold_loser=True,
        )
        self.assertFalse(keep)

    def test_empty_opposite_with_visible_loser_resets(self):
        fire, armed, why, leg = self._tick(0.03, 0.90, 10.0, None)
        fire, armed, why, _leg = self._tick(
            0.03, None, 12.0, armed, prev_leg=leg,
            last_status="error:no orders found to match with FAK order",
        )
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")


class SellHotPollTests(unittest.TestCase):
    """Ready→POST wait is the 5s poll plus mint-path work, not persist knobs.

    Live bag btc-updown-15m-1789880400: DN armed at 0.02, next sell tick
    ~10.7s later (poll_s=5 plus ~5s Gamma/positions after manage_sells).
    Pathlog: DN vanished at +1.5s and never returned, so persist 9s would
    still empty_keep_arm. Hot poll does not shorten persist; it samples
    every sell_hot_poll_s while armed so a persist-ready live bid is not
    delayed another full mint cycle.
    """

    _CFG = {
        "poll_s": 5.0,
        "sell_hot_poll_s": 1.0,
        "sell_persist_s": 9.0,
        "sell_persist_last_min_s": 5.0,
        "sell_persist_last_min_window_s": 60.0,
    }

    def _intent(self, **fields):
        base = {
            "status": "confirmed",
            "end_ts": 2_000_000.0,
            "sold_loser": False,
            "sold_winner": False,
        }
        base.update(fields)
        return base

    def test_idle_uses_poll_s(self):
        state = {"intents": {"a": self._intent()}}
        self.assertEqual(cycle_sleep_s(self._CFG, state, now_s=1_000_000.0), 5.0)
        self.assertFalse(skip_mint_discovery_for_sell(state, now_s=1_000_000.0))

    def test_loser_armed_uses_hot_poll_and_skips_mint_discovery(self):
        state = {
            "intents": {
                "a": self._intent(sell_loser_armed_at=1_000_000.0),
            }
        }
        now = 1_000_009.0
        self.assertTrue(sell_intent_hot(state["intents"]["a"], now))
        self.assertEqual(cycle_sleep_s(self._CFG, state, now_s=now), 1.0)
        self.assertTrue(skip_mint_discovery_for_sell(state, now_s=now))

    def test_empty_keep_arm_stays_hot_until_window_ends(self):
        # Armed + unsold loser inside the window (book may be empty).
        intent = self._intent(sell_loser_armed_at=10.0, sell_loser_leg="dn")
        self.assertTrue(sell_intent_hot(intent, now_s=20.0))
        expired = self._intent(
            sell_loser_armed_at=10.0, end_ts=15.0, sell_loser_leg="dn",
        )
        self.assertFalse(sell_intent_hot(expired, now_s=15.0))

    def test_sold_loser_clears_hot_unless_winner_or_dump_armed(self):
        sold = self._intent(
            sold_loser=True, sold_leg="dn", sell_loser_armed_at=10.0,
        )
        self.assertFalse(sell_intent_hot(sold, now_s=20.0))
        winner_arm = self._intent(
            sold_loser=True,
            sold_leg="dn",
            sell_winner_armed_at=18.0,
        )
        self.assertTrue(sell_intent_hot(winner_arm, now_s=20.0))
        dump_arm = self._intent(
            sold_loser=True,
            sold_leg="dn",
            sell_dump_armed_at=18.0,
        )
        self.assertTrue(sell_intent_hot(dump_arm, now_s=20.0))

    def test_hot_poll_does_not_change_persist_math(self):
        fire, armed, why = persist_ready(
            True, now_s=18.9, armed_ts=10.0, persist_s=9.0,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "waiting")
        fire, armed, why = persist_ready(
            True, now_s=18.9, armed_ts=10.0, persist_s=self._CFG["sell_persist_s"],
        )
        self.assertFalse(fire)
        fire, _, why = persist_ready(
            True, now_s=19.0, armed_ts=10.0, persist_s=self._CFG["sell_persist_s"],
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")
        fire, _, why = persist_ready(
            True, now_s=11.0, armed_ts=10.0, persist_s=1.0,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")
        # cycle_sleep_s must not be used as persist.
        self.assertEqual(self._CFG["sell_persist_s"], 9.0)
        self.assertEqual(self._CFG["sell_persist_last_min_s"], 5.0)

    def test_missing_hot_knob_falls_back_to_poll_s(self):
        cfg = {"poll_s": 5.0}
        state = {"intents": {"a": self._intent(sell_loser_armed_at=1.0)}}
        self.assertEqual(cycle_sleep_s(cfg, state, now_s=2.0), 5.0)


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
