"""Mint loser/winner sell policy (no CLOB posts, no mintbot import)."""

from __future__ import annotations

import unittest
from pathlib import Path

from buy.book import best_bid_with_min_size
from buy.mint_sell import (
    DEFAULT_SELL_KNOBS,
    classify_loser,
    cycle_sleep_s,
    depth_covers_size,
    dump_fast_retry_eligible,
    dump_retry_ladder_limits,
    mint_cycle_sleep_s,
    effective_loser_persist_s,
    empty_fak_status,
    inventory_latch,
    advance_oracle_edge_arm,
    late_oracle_edge_persist,
    late_oracle_need_usd,
    late_oracle_scrap_ok,
    loser_blind_fak_due,
    loser_empty_keep_qualify,
    loser_ladder_limits,
    loser_partial_fak_shares,
    loser_persist_ready,
    loser_scrap_persist_s,
    parse_sell_fill_shares,
    persist_ready,
    posted_order_id,
    rest_order_matched_shares,
    resting_tif,
    scrap_rest_action,
    sell_fire_decision,
    sell_intent_hot,
    sell_window_open,
    side_aware_oracle_edge_usd,
    skip_mint_discovery_for_sell,
    skip_mint_discovery_when_armed_and_capped,
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


class PersistLagFoldTests(unittest.TestCase):
    """Persist waits fold ~4s sell-tick/FAK lag so wall-clock stays ~9s / last-min ~5-6s."""

    def test_default_persist_ready_at_5s(self):
        persist = DEFAULT_SELL_KNOBS["sell_persist_s"]
        self.assertEqual(persist, 5.0)
        fire, _, why = persist_ready(
            True, now_s=14.9, armed_ts=10.0, persist_s=persist,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "waiting")
        fire, _, why = persist_ready(
            True, now_s=15.0, armed_ts=10.0, persist_s=persist,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")
        still_waiting, _, why_9 = persist_ready(
            True, now_s=18.9, armed_ts=10.0, persist_s=9.0,
        )
        self.assertFalse(still_waiting)
        self.assertEqual(why_9, "waiting")

    def test_default_last_min_persist_ready_at_2s(self):
        last = DEFAULT_SELL_KNOBS["sell_persist_last_min_s"]
        self.assertEqual(last, 2.0)
        fire, _, why = persist_ready(
            True, now_s=11.9, armed_ts=10.0, persist_s=last,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "waiting")
        fire, _, why = persist_ready(
            True, now_s=12.0, armed_ts=10.0, persist_s=last,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")

    def test_default_dump_persist_ready_at_2s(self):
        dump = DEFAULT_SELL_KNOBS["sell_dump_persist_s"]
        self.assertEqual(dump, 2.0)
        fire, _, why = persist_ready(
            True, now_s=11.9, armed_ts=10.0, persist_s=dump,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "waiting")
        fire, _, why = persist_ready(
            True, now_s=12.0, armed_ts=10.0, persist_s=dump,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")


class EffectiveLoserPersistTests(unittest.TestCase):
    """Last 60s before end_ts uses 2s persist so wait + lag ≈ last-min wall-clock."""

    _KNOBS = dict(persist_s=5.0, last_min_s=2.0, last_min_window_s=60.0)

    def test_ttm_over_60_uses_normal_5s(self):
        end = 10_000.0
        self.assertEqual(
            effective_loser_persist_s(now_s=end - 61.0, end_ts=end, **self._KNOBS),
            5.0,
        )
        self.assertEqual(
            effective_loser_persist_s(now_s=end - 90.0, end_ts=end, **self._KNOBS),
            5.0,
        )

    def test_ttm_30_uses_last_min_2s(self):
        end = 10_000.0
        self.assertEqual(
            effective_loser_persist_s(now_s=end - 30.0, end_ts=end, **self._KNOBS),
            2.0,
        )

    def test_ttm_exactly_60_uses_last_min(self):
        end = 10_000.0
        self.assertEqual(
            effective_loser_persist_s(now_s=end - 60.0, end_ts=end, **self._KNOBS),
            2.0,
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
        """Arm on 5s clock; last minute re-evaluates at 2s without resetting armed_ts.

        Arm at T-62 (5s). At T-60 elapsed is 2s: still waiting under 5s, ready
        under 2s, and armed_ts is unchanged (do not reset on the switch).
        """
        end = 10_000.0
        arm_now = end - 62.0
        tick_now = end - 60.0
        persist_at_arm = effective_loser_persist_s(
            now_s=arm_now, end_ts=end, **self._KNOBS
        )
        self.assertEqual(persist_at_arm, 5.0)
        fire, armed, why = persist_ready(
            True, now_s=arm_now, armed_ts=None, persist_s=persist_at_arm,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, arm_now)
        self.assertEqual(why, "armed")

        persist_inside = effective_loser_persist_s(
            now_s=tick_now, end_ts=end, **self._KNOBS
        )
        self.assertEqual(persist_inside, 2.0)
        still_waiting, armed_5, why_5 = persist_ready(
            True, now_s=tick_now, armed_ts=armed, persist_s=5.0,
        )
        self.assertFalse(still_waiting)
        self.assertEqual(armed_5, armed)
        self.assertEqual(why_5, "waiting")

        fire, armed_2, why_2 = persist_ready(
            True, now_s=tick_now, armed_ts=armed, persist_s=persist_inside,
        )
        self.assertTrue(fire)
        self.assertEqual(armed_2, armed)
        self.assertEqual(why_2, "ready")

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

    def test_fak_to_floor_includes_every_cent(self):
        """Joel: one fire walks 3¢ → 2¢ → 1¢, not only the endpoints."""
        self.assertEqual(
            loser_ladder_limits(
                threshold=0.03, floor=0.01, loser_bid=0.03, fak_px=0.03,
            ),
            [0.03, 0.02, 0.01],
        )

    def test_thinner_bid_starts_the_cent_ladder(self):
        self.assertEqual(
            loser_ladder_limits(
                threshold=0.03, floor=0.01, loser_bid=0.02, fak_px=0.03,
            ),
            [0.02, 0.01],
        )
        self.assertEqual(
            loser_ladder_limits(
                threshold=0.03, floor=0.01, loser_bid=0.01, fak_px=0.03,
            ),
            [0.01],
        )

    def test_bid_below_one_cent_floor_stays_the_live_bid(self):
        self.assertEqual(
            loser_ladder_limits(
                threshold=0.03, floor=0.01, loser_bid=0.005, fak_px=0.03,
            ),
            [0.005],
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


class DumpFastRetryTests(unittest.TestCase):
    def test_fast_retry_triggers_for_empty_fak_or_kill_zero_fill(self):
        self.assertTrue(
            dump_fast_retry_eligible(
                sold=0.0,
                status="error:no orders found to match with FAK order",
                tol=0.01,
            )
        )
        self.assertTrue(
            dump_fast_retry_eligible(
                sold=0.0,
                status="killed",
                tol=0.01,
            )
        )
        self.assertTrue(
            dump_fast_retry_eligible(
                sold=0.0,
                status="cancelled",
                tol=0.01,
            )
        )

    def test_fast_retry_stops_on_fill_or_non_retryable_status(self):
        self.assertFalse(
            dump_fast_retry_eligible(
                sold=0.05,
                status="error:no orders found to match with FAK order",
                tol=0.01,
            )
        )
        self.assertFalse(
            dump_fast_retry_eligible(sold=0.0, status="matched", tol=0.01)
        )
        self.assertFalse(
            dump_fast_retry_eligible(sold=0.0, status="error:timeout", tol=0.01)
        )

    def test_retry_ladder_descends_from_top_bid_toward_floor(self):
        self.assertEqual(
            dump_retry_ladder_limits(0.11, floor=0.02, step=0.04, max_rungs=4),
            [0.11, 0.07, 0.03, 0.02],
        )

    def test_retry_ladder_clamps_to_live_bid_when_below_floor(self):
        self.assertEqual(
            dump_retry_ladder_limits(0.015, floor=0.02, step=0.01, max_rungs=3),
            [0.015],
        )
        self.assertEqual(
            dump_retry_ladder_limits(0.0, floor=0.02, step=0.01, max_rungs=3),
            [],
        )


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


class SellArmedPollTests(unittest.TestCase):
    """Armed-fast poll is sell-loop sleep, not persist knobs and not skip-mint.

    VM audit bag btc-updown-15m-1789880400: poll_s=5, effective sell tick
    ≈8.6–10.5s. Armed DN@0.02; persist ready +9s; first empty_keep_arm at
    +10.7s; never FAKed. Miss = empty book on first post-ready look + coarse
    tick, not a stuck POST. ROI is catching fleeting 2¢ books after persist.

    Incident btc-updown-15m-1789905600: after loser_done the bag went cold
    and next-window mint stole the serial cycle. Sell stays hot through
    dump/winner exit; mint_cycle_sleep_s stays poll_s.
    """

    _CFG = {
        "poll_s": 5.0,
        "sell_armed_poll_s": 2.0,
        "sell_persist_s": 5.0,
        "sell_persist_last_min_s": 2.0,
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

    def test_loser_armed_selects_armed_poll(self):
        state = {
            "intents": {
                "a": self._intent(sell_loser_armed_at=1_000_000.0),
            }
        }
        now = 1_000_009.0
        self.assertTrue(sell_intent_hot(state["intents"]["a"], now))
        self.assertEqual(cycle_sleep_s(self._CFG, state, now_s=now), 2.0)
        self.assertTrue(skip_mint_discovery_for_sell(state, now_s=now))
        self.assertTrue(
            skip_mint_discovery_when_armed_and_capped(
                loser_armed=True, mint_capped=True,
            )
        )
        self.assertFalse(
            skip_mint_discovery_when_armed_and_capped(
                loser_armed=True, mint_capped=False,
            )
        )
        self.assertFalse(
            skip_mint_discovery_when_armed_and_capped(
                loser_armed=False, mint_capped=True,
            )
        )

    def test_armed_poll_may_be_below_poll_s_floor(self):
        cfg = {**self._CFG, "poll_s": 5.0, "sell_armed_poll_s": 2.0}
        state = {"intents": {"a": self._intent(sell_loser_armed_at=1.0)}}
        self.assertEqual(cycle_sleep_s(cfg, state, now_s=2.0), 2.0)
        self.assertGreaterEqual(cfg["poll_s"], 2.0)
        self.assertLess(cfg["sell_armed_poll_s"], cfg["poll_s"])

    def test_empty_keep_arm_stays_hot_until_window_ends(self):
        intent = self._intent(sell_loser_armed_at=10.0, sell_loser_leg="dn")
        self.assertTrue(sell_intent_hot(intent, now_s=20.0))
        expired = self._intent(
            sell_loser_armed_at=10.0, end_ts=15.0, sell_loser_leg="dn",
        )
        self.assertFalse(sell_intent_hot(expired, now_s=15.0))

    def test_sold_loser_stays_hot_until_dump_or_winner_done(self):
        sold = self._intent(
            sold_loser=True, sold_leg="dn", sell_loser_armed_at=10.0,
        )
        self.assertTrue(sell_intent_hot(sold, now_s=20.0))
        self.assertEqual(
            cycle_sleep_s(self._CFG, {"intents": {"a": sold}}, now_s=20.0),
            2.0,
        )
        winner_arm = self._intent(
            sold_loser=True,
            sold_leg="dn",
            sell_winner_armed_at=18.0,
        )
        self.assertTrue(sell_intent_hot(winner_arm, now_s=20.0))
        self.assertEqual(
            cycle_sleep_s(
                self._CFG, {"intents": {"a": winner_arm}}, now_s=20.0,
            ),
            2.0,
        )
        dump_arm = self._intent(
            sold_loser=True,
            sold_leg="dn",
            sell_dump_armed_at=18.0,
        )
        self.assertTrue(sell_intent_hot(dump_arm, now_s=20.0))
        done = self._intent(
            sold_loser=True,
            sold_leg="dn",
            sold_dump=True,
            sold_winner=True,
        )
        self.assertFalse(sell_intent_hot(done, now_s=20.0))
        self.assertEqual(
            cycle_sleep_s(self._CFG, {"intents": {"a": done}}, now_s=20.0),
            5.0,
        )
        self.assertEqual(mint_cycle_sleep_s(self._CFG), 5.0)
        hot_state = {"intents": {"a": sold}}
        self.assertTrue(skip_mint_discovery_for_sell(hot_state, now_s=20.0))
        self.assertEqual(mint_cycle_sleep_s(self._CFG), self._CFG["poll_s"])

    def test_armed_poll_does_not_change_persist_math(self):
        fire, armed, why = persist_ready(
            True, now_s=14.9, armed_ts=10.0, persist_s=5.0,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "waiting")
        fire, _, why = persist_ready(
            True, now_s=15.0, armed_ts=10.0, persist_s=self._CFG["sell_persist_s"],
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")
        self.assertEqual(self._CFG["sell_persist_s"], 5.0)
        self.assertEqual(self._CFG["sell_persist_last_min_s"], 2.0)

    def test_missing_armed_knob_falls_back_to_poll_s(self):
        cfg = {"poll_s": 5.0}
        state = {"intents": {"a": self._intent(sell_loser_armed_at=1.0)}}
        self.assertEqual(cycle_sleep_s(cfg, state, now_s=2.0), 5.0)


class SellFireDecisionTests(unittest.TestCase):
    """At FAK time, cancel if the path is no longer in range; do not POST."""

    def test_loser_cancels_when_bid_rose_above_threshold(self):
        fire, _, why = persist_ready(
            True, now_s=15.0, armed_ts=10.0, persist_s=5.0,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")
        action, reason = sell_fire_decision(
            "loser", bid=0.04, opposite_bid=0.95, threshold=0.03, opposite_min=0.90,
        )
        self.assertEqual(action, "cancel_reset")
        self.assertEqual(reason, "loser_above_threshold")

    def test_loser_fires_when_still_in_range_including_below_floor(self):
        action, reason = sell_fire_decision(
            "loser", bid=0.03, opposite_bid=0.92, threshold=0.03, opposite_min=0.90,
        )
        self.assertEqual(action, "fire")
        self.assertEqual(reason, "loser")
        action, reason = sell_fire_decision(
            "loser", bid=0.01, opposite_bid=0.97, threshold=0.03, floor=0.02,
            opposite_min=0.90,
        )
        self.assertEqual(action, "fire")
        self.assertEqual(reason, "loser")

    def test_loser_empty_book_keeps_arm_and_does_not_fire(self):
        action, reason = sell_fire_decision(
            "loser", bid=None, opposite_bid=0.95, threshold=0.03, opposite_min=0.90,
        )
        self.assertEqual(action, "cancel_keep_arm")
        self.assertEqual(reason, "empty_book")

    def test_loser_still_requires_opposite_min(self):
        action, reason = sell_fire_decision(
            "loser", bid=0.02, opposite_bid=0.80, threshold=0.03, opposite_min=0.90,
        )
        self.assertEqual(action, "cancel_reset")
        self.assertEqual(reason, "wick_unconfirmed")
        action, reason = sell_fire_decision(
            "loser", bid=0.02, opposite_bid=None, threshold=0.03, opposite_min=0.90,
        )
        self.assertEqual(action, "cancel_reset")
        self.assertEqual(reason, "wick_unconfirmed")

    def test_dump_cancels_when_bid_rises_to_or_above_below(self):
        fire, _, why = persist_ready(
            True, now_s=12.0, armed_ts=10.0, persist_s=2.0,
        )
        self.assertTrue(fire)
        action, reason = sell_fire_decision("dump", bid=0.80, dump_below=0.80)
        self.assertEqual(action, "cancel_reset")
        self.assertEqual(reason, "dump_at_or_above_below")
        action, reason = sell_fire_decision("dump", bid=0.81, dump_below=0.80)
        self.assertEqual(action, "cancel_reset")
        self.assertEqual(reason, "dump_at_or_above_below")

    def test_dump_fires_when_still_below_and_cancels_empty(self):
        action, reason = sell_fire_decision("dump", bid=0.74, dump_below=0.80)
        self.assertEqual(action, "fire")
        self.assertEqual(reason, "dump")
        action, reason = sell_fire_decision("dump", bid=None, dump_below=0.80)
        self.assertEqual(action, "cancel_reset")
        self.assertEqual(reason, "empty_book")

    def test_winner_cheap_cancels_when_bid_drops_below_min(self):
        fire, _, why = persist_ready(
            True, now_s=15.0, armed_ts=10.0, persist_s=5.0,
        )
        self.assertTrue(fire)
        action, reason = sell_fire_decision(
            "winner", bid=0.97, winner_min=0.99, cheap_on=True,
        )
        self.assertEqual(action, "cancel_reset")
        self.assertEqual(reason, "winner_below_min")

    def test_winner_cheap_fires_when_still_allowed(self):
        action, reason = sell_fire_decision(
            "winner", bid=0.995, winner_min=0.99, cheap_on=True,
        )
        self.assertEqual(action, "fire")
        self.assertEqual(reason, "winner_cheap")
        action, reason = sell_fire_decision(
            "winner", bid=0.999, winner_min=0.999, cheap_on=False,
        )
        self.assertEqual(action, "fire")
        self.assertEqual(reason, "winner")
        action, reason = sell_fire_decision(
            "winner", bid=0.99, winner_min=0.999, cheap_on=False,
        )
        self.assertEqual(action, "cancel_reset")
        self.assertEqual(reason, "winner_below_min")


class LateOracleScrapGateTests(unittest.TestCase):
    """Combat for true reverse btc-updown-15m-1790078400."""

    def test_default_threshold_is_two_cents(self):
        thr = DEFAULT_SELL_KNOBS["sell_threshold"]
        floor = DEFAULT_SELL_KNOBS["sell_floor"]
        fak = DEFAULT_SELL_KNOBS["sell_fak_px"]
        self.assertEqual(thr, 0.02)
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_persist_s"], 5.0)
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_persist_last_min_s"], 2.0)
        self.assertIs(DEFAULT_SELL_KNOBS["sell_persist_skip_when_sized"], False)
        self.assertNotIn("sell_dump_if_sister_miss_s", DEFAULT_SELL_KNOBS)
        self.assertEqual(floor, 0.02)
        self.assertEqual(fak, 0.02)
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_scrap_rest_px"], 0.02)
        self.assertTrue(floor <= fak <= thr)
        leg, why = classify_loser(
            up_bid=0.02, dn_bid=0.95,
            threshold=thr,
            opposite_min=DEFAULT_SELL_KNOBS["sell_opposite_min"],
        )
        self.assertEqual((leg, why), ("up", "loser"))
        leg, why = classify_loser(
            up_bid=0.03, dn_bid=0.95,
            threshold=thr,
            opposite_min=DEFAULT_SELL_KNOBS["sell_opposite_min"],
        )
        self.assertEqual(why, "none")
        # 2¢ arms and prints. Fak equals the floor, so the ladder is one rung.
        self.assertEqual(
            loser_ladder_limits(
                threshold=thr, floor=floor, loser_bid=0.02, fak_px=fak,
            ),
            [0.02],
        )
        self.assertEqual(
            loser_ladder_limits(
                threshold=thr, floor=floor, loser_bid=0.01, fak_px=fak,
            ),
            [0.01],
        )

    def test_defaults_include_late_oracle_knobs(self):
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_late_window_s"], 0.0)
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_oracle_edge_per_ttm"], 0.0)
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_oracle_edge_persist_s"], 3.0)
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_oracle_stale_s"], 0.0)
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_oracle_edge_floor_usd"], 0.0)

    def test_side_aware_edge_scraping_dn_keeps_up(self):
        # Scraping Down (keeping Up): need twap - open.
        self.assertAlmostEqual(
            side_aware_oracle_edge_usd(
                twap_usd=100_020.0, open_usd=100_000.0, scrap_leg="dn"
            ),
            20.0,
        )
        self.assertAlmostEqual(
            side_aware_oracle_edge_usd(
                twap_usd=99_980.0, open_usd=100_000.0, scrap_leg="up"
            ),
            20.0,
        )

    def test_need_uses_floor_and_per_ttm(self):
        # TTM 42 → 1.5*42 = 63 > floor 25.
        self.assertAlmostEqual(late_oracle_need_usd(42.0), 63.0)
        # TTM 10 → 15 < floor 25.
        self.assertAlmostEqual(late_oracle_need_usd(10.0), 25.0)

    def test_ttm_42_edge_20_scraping_dn_blocks(self):
        # 1790078400: TTM≈42 need ≳$63; actual edge ~+$20 → block.
        ok, why, detail = late_oracle_scrap_ok(
            ttm_s=42.0,
            scrap_leg="dn",
            twap_usd=100_020.0,
            open_usd=100_000.0,
            twap_age_s=1.0,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "edge_thin")
        self.assertAlmostEqual(detail["edge"], 20.0)
        self.assertAlmostEqual(detail["need"], 63.0)

    def test_ttm_42_edge_70_allows_after_persist(self):
        ok, why, detail = late_oracle_scrap_ok(
            ttm_s=42.0,
            scrap_leg="dn",
            twap_usd=100_070.0,
            open_usd=100_000.0,
            twap_age_s=0.5,
        )
        self.assertTrue(ok)
        self.assertEqual(why, "edge_ok")
        self.assertAlmostEqual(detail["edge"], 70.0)
        self.assertAlmostEqual(detail["need"], 63.0)
        # CLOB would allow separately; oracle needs 3s persist.
        fire, armed, why_p = late_oracle_edge_persist(
            True, now_s=100.0, armed_ts=None, persist_s=3.0
        )
        self.assertFalse(fire)
        self.assertEqual(why_p, "armed")
        fire, armed, why_p = late_oracle_edge_persist(
            True, now_s=101.5, armed_ts=armed, persist_s=3.0
        )
        self.assertFalse(fire)
        self.assertEqual(why_p, "waiting")
        fire, armed, why_p = late_oracle_edge_persist(
            True, now_s=103.0, armed_ts=armed, persist_s=3.0
        )
        self.assertTrue(fire)
        self.assertEqual(why_p, "ready")

    def test_empty_loser_book_does_not_reset_oracle_arm(self):
        # Bag btc-updown-15m-1790210700: edge already OK, then the ≤2¢ bid
        # flickered off. Persist must keep counting on the kept leg.
        fire, armed, why, leg = advance_oracle_edge_arm(
            edge_ok=True,
            sold_loser=False,
            scrap_leg="up",
            now_s=100.0,
            armed_ts=None,
            armed_leg=None,
            persist_s=3.0,
            in_late=True,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "armed")
        self.assertEqual(leg, "up")
        fire, armed, why, leg = advance_oracle_edge_arm(
            edge_ok=True,
            sold_loser=False,
            scrap_leg="up",
            now_s=102.0,
            armed_ts=armed,
            armed_leg=leg,
            persist_s=3.0,
            in_late=True,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "waiting")
        self.assertEqual(armed, 100.0)
        fire, armed, why, leg = advance_oracle_edge_arm(
            edge_ok=True,
            sold_loser=False,
            scrap_leg="up",
            now_s=103.0,
            armed_ts=armed,
            armed_leg=leg,
            persist_s=3.0,
            in_late=True,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")
        self.assertEqual(leg, "up")

    def test_oracle_arm_still_resets_on_thin_edge_sold_window_and_leg_flip(self):
        fire, armed, why, leg = advance_oracle_edge_arm(
            edge_ok=False,
            sold_loser=False,
            scrap_leg="up",
            now_s=103.0,
            armed_ts=100.0,
            armed_leg="up",
            persist_s=3.0,
            in_late=True,
        )
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")
        self.assertIsNone(leg)
        fire, armed, why, leg = advance_oracle_edge_arm(
            edge_ok=True,
            sold_loser=True,
            scrap_leg="up",
            now_s=103.0,
            armed_ts=100.0,
            armed_leg="up",
            persist_s=3.0,
            in_late=True,
        )
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")
        fire, armed, why, leg = advance_oracle_edge_arm(
            edge_ok=True,
            sold_loser=False,
            scrap_leg="up",
            now_s=103.0,
            armed_ts=100.0,
            armed_leg="up",
            persist_s=3.0,
            in_late=False,
        )
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")
        fire, armed, why, leg = advance_oracle_edge_arm(
            edge_ok=True,
            sold_loser=False,
            scrap_leg=None,
            now_s=103.0,
            armed_ts=100.0,
            armed_leg="up",
            persist_s=3.0,
            in_late=True,
        )
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")
        fire, armed, why, leg = advance_oracle_edge_arm(
            edge_ok=True,
            sold_loser=False,
            scrap_leg="dn",
            now_s=101.0,
            armed_ts=100.0,
            armed_leg="up",
            persist_s=3.0,
            in_late=True,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "armed")
        self.assertEqual(armed, 101.0)
        self.assertEqual(leg, "dn")

    def test_zero_late_window_skips_gate_even_with_missing_tape(self):
        ok, why, detail = late_oracle_scrap_ok(
            ttm_s=42.0,
            scrap_leg="dn",
            twap_usd=None,
            open_usd=None,
            twap_age_s=None,
            late_window_s=DEFAULT_SELL_KNOBS["sell_late_window_s"],
        )
        self.assertTrue(ok)
        self.assertEqual(why, "outside_late_window")
        self.assertIsNone(detail["need"])

    def test_ttm_200_oracle_gate_not_applied(self):
        ok, why, _ = late_oracle_scrap_ok(
            ttm_s=200.0,
            scrap_leg="dn",
            twap_usd=None,
            open_usd=None,
            twap_age_s=None,
        )
        self.assertTrue(ok)
        self.assertEqual(why, "outside_late_window")

    def test_missing_or_stale_oracle_in_late_window_blocks(self):
        ok, why, _ = late_oracle_scrap_ok(
            ttm_s=42.0,
            scrap_leg="dn",
            twap_usd=100_070.0,
            open_usd=None,
            twap_age_s=1.0,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "missing_open")
        ok, why, _ = late_oracle_scrap_ok(
            ttm_s=42.0,
            scrap_leg="dn",
            twap_usd=None,
            open_usd=100_000.0,
            twap_age_s=1.0,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "missing_twap")
        ok, why, _ = late_oracle_scrap_ok(
            ttm_s=42.0,
            scrap_leg="dn",
            twap_usd=100_070.0,
            open_usd=100_000.0,
            twap_age_s=6.0,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "stale_twap")

    def test_wrong_sign_edge_scraping_dn_blocks(self):
        # Scraping DN but twap below open → negative edge.
        ok, why, detail = late_oracle_scrap_ok(
            ttm_s=42.0,
            scrap_leg="dn",
            twap_usd=99_980.0,
            open_usd=100_000.0,
            twap_age_s=1.0,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "edge_thin")
        self.assertLess(detail["edge"], 0.0)


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


class ScrapSpeedTests(unittest.TestCase):
    """Skip-persist, blind 1¢ FAK, and post-miss resting scrap."""

    _END = 1_790_101_800.0  # btc-updown-15m-1790100900 closes 900s after slug

    def _persist(self, *, now_s, depth=0.0, size=50.0, skip_ttm=90.0, sized=True):
        return loser_scrap_persist_s(
            now_s=now_s,
            end_ts=self._END,
            persist_s=2.5,
            last_min_s=1.0,
            last_min_window_s=60.0,
            skip_ttm_s=skip_ttm,
            depth_at_limit=depth,
            our_size=size,
            skip_when_sized=sized,
        )

    def test_last_90s_skips_persist(self):
        persist, why = self._persist(now_s=self._END - 70.0, depth=0.0)
        self.assertEqual(persist, 0.0)
        self.assertEqual(why, "late_skip")
        persist, why = self._persist(now_s=self._END - 90.0, depth=0.0)
        self.assertEqual((persist, why), (0.0, "late_skip"))
        persist, why = self._persist(now_s=self._END - 100.0, depth=0.0)
        self.assertEqual((persist, why), (2.5, "normal"))

    def test_sized_depth_skips_persist(self):
        self.assertTrue(depth_covers_size(394.0, 50.0))
        self.assertTrue(depth_covers_size(50.0, 50.0))
        self.assertFalse(depth_covers_size(10.0, 50.0))
        self.assertFalse(depth_covers_size(None, 50.0))
        # Incident shape: ~4m TTM, book shows more size than we hold.
        persist, why = self._persist(now_s=self._END - 240.0, depth=394.0, size=50.0)
        self.assertEqual((persist, why), (0.0, "sized_skip"))
        persist, why = self._persist(now_s=self._END - 240.0, depth=10.0, size=50.0)
        self.assertEqual((persist, why), (2.5, "normal"))
        # Default knob is off: a fat book still waits the full persist.
        persist, why = loser_scrap_persist_s(
            now_s=self._END - 240.0,
            end_ts=self._END,
            persist_s=5.0,
            last_min_s=2.0,
            last_min_window_s=60.0,
            skip_ttm_s=90.0,
            depth_at_limit=394.0,
            our_size=50.0,
            skip_when_sized=False,
        )
        self.assertEqual((persist, why), (5.0, "normal"))

    def test_partial_fak_clips_to_short_depth(self):
        self.assertEqual(
            loser_partial_fak_shares(remaining=50.0, depth_at_limit=10.0),
            10.0,
        )
        self.assertEqual(
            loser_partial_fak_shares(remaining=50.0, depth_at_limit=80.0),
            50.0,
        )
        self.assertEqual(
            loser_partial_fak_shares(remaining=50.0, depth_at_limit=0.0),
            50.0,
        )

    def test_blind_fak_on_empty_keep_respects_backoff(self):
        fire, why = loser_blind_fak_due(
            why="empty_keep_arm",
            now_s=10.0,
            last_blind_at=None,
            backoff_s=3.0,
            sold_loser=False,
            has_inventory=True,
            rest_live=False,
            oracle_blocks=False,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "blind_fak")
        fire, why = loser_blind_fak_due(
            why="empty_fak_keep_arm",
            now_s=12.0,
            last_blind_at=10.0,
            backoff_s=3.0,
            sold_loser=False,
            has_inventory=True,
            rest_live=False,
            oracle_blocks=False,
        )
        self.assertFalse(fire)
        self.assertEqual(why, "backoff")
        fire, why = loser_blind_fak_due(
            why="empty_keep_arm",
            now_s=13.0,
            last_blind_at=10.0,
            backoff_s=3.0,
            sold_loser=False,
            has_inventory=True,
            rest_live=False,
            oracle_blocks=False,
        )
        self.assertTrue(fire)
        fire, why = loser_blind_fak_due(
            why="empty_keep_arm",
            now_s=20.0,
            last_blind_at=None,
            backoff_s=3.0,
            sold_loser=False,
            has_inventory=True,
            rest_live=True,
            oracle_blocks=False,
        )
        self.assertEqual(why, "rest_live")

    def test_fak_miss_places_rest_and_window_end_cancels(self):
        action, why = scrap_rest_action(
            enabled=True,
            rest_order_id=None,
            sold_loser=False,
            window_open=True,
            loser_qualifies=True,
            oracle_blocks=False,
            fak_miss=True,
            armed=True,
        )
        self.assertEqual((action, why), ("place", "fak_miss"))
        # 1790100900: FAK miss with minutes left → GTD through the window.
        now = self._END - 240.0
        tif, exp = resting_tif(now_s=now, expire_ts=self._END, min_ahead_s=60.0)
        self.assertEqual((tif, exp), ("GTD", int(self._END)))
        action, why = scrap_rest_action(
            enabled=True,
            rest_order_id="oid-1",
            sold_loser=False,
            window_open=False,
            loser_qualifies=True,
            oracle_blocks=False,
            fak_miss=True,
            armed=True,
        )
        self.assertEqual((action, why), ("cancel", "window_end"))
        action, why = scrap_rest_action(
            enabled=True,
            rest_order_id="oid-1",
            sold_loser=False,
            window_open=True,
            loser_qualifies=False,
            oracle_blocks=False,
            fak_miss=True,
            armed=True,
        )
        self.assertEqual((action, why), ("cancel", "loser_disqualified"))
        self.assertEqual(posted_order_id({"orderID": "abc"}), "abc")
        matched, status = rest_order_matched_shares(
            {"status": "live", "size_matched": "0"},
            50.0,
        )
        self.assertEqual((matched, status), (0.0, "live"))
        matched, status = rest_order_matched_shares(
            {"status": "matched", "size_matched": "50"},
            50.0,
        )
        self.assertEqual(status, "filled")

    def test_gtd_falls_back_to_gtc_inside_security_window(self):
        tif, exp = resting_tif(now_s=1_000.0, expire_ts=1_030.0, min_ahead_s=60.0)
        self.assertEqual((tif, exp), ("GTC", 0))

    def test_resting_tif_default_is_gtc_inside_180s_and_gtd_farther(self):
        # btc-updown-15m-1790180100: ~90s left posted GTD and the exchange
        # rejected it. Polymarket's GTD floor is ~180s.
        tif, exp = resting_tif(now_s=1_000.0, expire_ts=1_090.0)
        self.assertEqual((tif, exp), ("GTC", 0))
        tif, exp = resting_tif(now_s=1_000.0, expire_ts=1_179.0)
        self.assertEqual((tif, exp), ("GTC", 0))
        tif, exp = resting_tif(now_s=1_000.0, expire_ts=1_240.0)
        self.assertEqual((tif, exp), ("GTD", 1240))
        self.assertEqual(DEFAULT_SELL_KNOBS["sell_scrap_rest_min_ahead_s"], 180.0)

    def test_scrap_rest_px_caps_to_live_bid(self):
        from buy.mint_sell import scrap_rest_px

        self.assertEqual(scrap_rest_px(0.02, 0.01), 0.01)
        self.assertEqual(scrap_rest_px(0.02, None), 0.02)
        self.assertEqual(scrap_rest_px(0.02, 0.05), 0.02)
        self.assertEqual(scrap_rest_px(0.02, 0), 0.02)

    def test_thin_oracle_still_blocks_when_persist_is_skipped(self):
        ok, why, _detail = late_oracle_scrap_ok(
            ttm_s=42.0,
            scrap_leg="dn",
            twap_usd=100_020.0,
            open_usd=100_000.0,
            twap_age_s=1.0,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "edge_thin")
        persist, skip_why = self._persist(now_s=self._END - 42.0, depth=400.0)
        self.assertEqual((persist, skip_why), (0.0, "late_skip"))
        fire, blind_why = loser_blind_fak_due(
            why="empty_keep_arm",
            now_s=self._END - 42.0,
            last_blind_at=None,
            backoff_s=3.0,
            sold_loser=False,
            has_inventory=True,
            rest_live=False,
            oracle_blocks=True,
        )
        self.assertFalse(fire)
        self.assertEqual(blind_why, "oracle_block")
        action, rest_why = scrap_rest_action(
            enabled=True,
            rest_order_id=None,
            sold_loser=False,
            window_open=True,
            loser_qualifies=True,
            oracle_blocks=True,
            fak_miss=True,
            armed=True,
        )
        self.assertEqual((action, rest_why), ("none", "oracle_block"))
        action, rest_why = scrap_rest_action(
            enabled=True,
            rest_order_id="oid-live",
            sold_loser=False,
            window_open=True,
            loser_qualifies=True,
            oracle_blocks=True,
            fak_miss=True,
            armed=True,
        )
        self.assertEqual((action, rest_why), ("cancel", "oracle_block"))


class SisterMissDumpRemovedTests(unittest.TestCase):
    def test_policy_module_has_no_sister_miss_dump(self):
        src = Path(__file__).resolve().parents[1].joinpath("buy", "mint_sell.py").read_text()
        self.assertNotIn("def sister_hedge_dump_due", src)
        self.assertNotIn("sell_dump_if_sister_miss_s", src)


if __name__ == "__main__":
    unittest.main()
