"""Unit tests for the 5m persist hedge gate (no network)."""

from __future__ import annotations

import unittest

from buy.hedge_gate import (
    PERSIST_BOUNCE_MAX,
    clob_min_tick_from_error,
    evaluate_held_bag,
    hedge_market_tick,
    hedge_persist_ready,
    hedge_should_keep_retrying,
    hedge_tick_after_build_error,
    persist_should_cancel_on_bid,
)


class HedgePersistReadyTests(unittest.TestCase):
    def test_reset_when_book_fails(self):
        fire, armed, why = hedge_persist_ready(
            False, now_s=10.0, armed_ts=8.0, persist_s=2.0,
        )
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")

    def test_immediate_when_persist_off(self):
        fire, armed, why = hedge_persist_ready(
            True, now_s=10.0, armed_ts=None, persist_s=0.0,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "immediate")

    def test_toxic_skips_persist(self):
        fire, _armed, why = hedge_persist_ready(
            True, now_s=10.0, armed_ts=None, persist_s=2.0, toxic=True,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "immediate")

    def test_arms_on_first_qualify(self):
        fire, armed, why = hedge_persist_ready(
            True, now_s=10.0, armed_ts=None, persist_s=2.0,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "armed")

    def test_waits_until_persist_elapsed(self):
        fire, armed, why = hedge_persist_ready(
            True, now_s=11.5, armed_ts=10.0, persist_s=2.0,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "waiting")

    def test_fires_after_persist(self):
        fire, armed, why = hedge_persist_ready(
            True, now_s=12.0, armed_ts=10.0, persist_s=2.0,
        )
        self.assertTrue(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "ready")


class HedgeMarketTickTests(unittest.TestCase):
    """22 Aug 11:40: persist fired, CLOB rejected 0.001 on a 0.01 book."""

    def test_honors_coarser_clob_tick(self):
        self.assertEqual(hedge_market_tick("0.01", "0.001"), 0.01)
        self.assertEqual(hedge_market_tick(0.01, 0.001), 0.01)
        self.assertEqual(hedge_market_tick("0.001", "0.001"), 0.001)

    def test_missing_or_junk_falls_back_to_expected(self):
        self.assertEqual(hedge_market_tick(None, "0.001"), 0.001)
        self.assertEqual(hedge_market_tick("", 0.001), 0.001)
        self.assertEqual(hedge_market_tick("nope", "0.001"), 0.001)
        self.assertEqual(hedge_market_tick(-1, "0.001"), 0.001)

    def test_parse_invalid_tick_minimum(self):
        err = "PolyApiException[status_code=400, error_message=invalid tick size (0.001), minimum is 0.01]"
        self.assertEqual(clob_min_tick_from_error(err), 0.01)
        self.assertIsNone(clob_min_tick_from_error("no orders found to match"))
        self.assertIsNone(clob_min_tick_from_error(""))

    def test_retry_only_when_minimum_is_coarser(self):
        err = "invalid tick size (0.001), minimum is 0.01"
        self.assertEqual(hedge_tick_after_build_error("0.001", err), 0.01)
        self.assertEqual(hedge_tick_after_build_error(0.001, err), 0.01)
        self.assertIsNone(hedge_tick_after_build_error("0.01", err))
        self.assertIsNone(hedge_tick_after_build_error("0.001", "no orders found to match"))


class WinnerRallyIsNotAHedgeTests(unittest.TestCase):
    """22 Aug 1:35–1:40PM ET: sold a Down winner at 0.93/0.94 as a reversal."""

    def test_cancel_ws_peek_on_093_even_after_persist(self):
        self.assertTrue(
            persist_should_cancel_on_bid(0.93, persist_done=True)
        )
        self.assertTrue(
            persist_should_cancel_on_bid(0.99, persist_done=True)
        )
        self.assertFalse(
            persist_should_cancel_on_bid(0.74, persist_done=True)
        )
        self.assertTrue(
            persist_should_cancel_on_bid(0.74, persist_done=False)
        )
        self.assertFalse(
            persist_should_cancel_on_bid(0.70, persist_done=False)
        )
        self.assertAlmostEqual(PERSIST_BOUNCE_MAX, 0.80)

    def test_persist_2s_at_70_72_still_fires(self):
        fire, _armed, why = hedge_persist_ready(
            True, now_s=12.0, armed_ts=10.0, persist_s=2.0,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "ready")
        intent = evaluate_held_bag(
            0.70, 0.72, now_s=12.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=False, gui_ok=True,
        )
        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.reason, "persist_ready")

    def test_toxic_dump_053_still_fires(self):
        intent = evaluate_held_bag(
            0.53, 0.60, now_s=1.0, persist_armed_ts=None, persist_s=2.0,
            persist_done=False, gui_ok=False,
        )
        self.assertEqual(intent.action, "dump")
        self.assertTrue(intent.dump)
        self.assertAlmostEqual(intent.sell_at, 0.53)

    def test_retry_stops_when_live_bid_is_093(self):
        self.assertFalse(
            hedge_should_keep_retrying(3.13, 0.93, persist_done=True)
        )
        self.assertTrue(
            hedge_should_keep_retrying(3.13, 0.74, persist_done=True)
        )


if __name__ == "__main__":
    unittest.main()
