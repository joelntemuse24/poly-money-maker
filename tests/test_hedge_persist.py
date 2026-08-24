"""Unit tests for the 5m persist hedge gate (no network)."""

from __future__ import annotations

import unittest

from buy.hedge_gate import (
    clob_min_tick_from_error,
    evaluate_held_bag,
    hedge_market_tick,
    hedge_persist_ready,
    hedge_should_keep_retrying,
    hedge_tick_after_build_error,
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


class HeldBagRecoveryCancelTests(unittest.TestCase):
    """Persist-done must not sell a recovered 90–99¢ winner."""

    def test_persist_done_bid_099_holds_and_clears(self):
        intent = evaluate_held_bag(
            0.99, 0.99, now_s=20.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=True,
        )
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.reason, "recovery_cancel")
        self.assertFalse(intent.persist_done)
        self.assertIsNone(intent.persist_ts)
        self.assertNotEqual(intent.action, "sell")

    def test_persist_done_bid_078_sells_live_bid(self):
        intent = evaluate_held_bag(
            0.78, 0.80, now_s=20.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=True,
        )
        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.reason, "persist_live_bid")
        self.assertAlmostEqual(intent.sell_at, 0.78)
        self.assertAlmostEqual(intent.abort_above, 0.85)
        self.assertTrue(intent.persist_done)

    def test_persist_done_bid_060_dead_band(self):
        intent = evaluate_held_bag(
            0.60, 0.62, now_s=20.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=True,
        )
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.reason, "dead_band")
        self.assertTrue(intent.persist_done)

    def test_bid_050_dumps_regardless_of_persist(self):
        intent = evaluate_held_bag(
            0.50, 0.52, now_s=20.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=True, gui_ok=False,
        )
        self.assertEqual(intent.action, "dump")
        self.assertTrue(intent.dump)
        self.assertTrue(intent.skip_gui)
        self.assertAlmostEqual(intent.sell_at, 0.50)

    def test_arm_2s_then_bid_095_before_sell_holds_and_clears(self):
        armed = evaluate_held_bag(
            0.70, 0.72, now_s=10.0, persist_armed_ts=None, persist_s=2.0,
            persist_done=False, gui_ok=True,
        )
        self.assertEqual(armed.action, "arm")
        intent = evaluate_held_bag(
            0.95, 0.96, now_s=12.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=False, gui_ok=True,
        )
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.reason, "recovery_cancel")
        self.assertFalse(intent.persist_done)
        self.assertIsNone(intent.persist_ts)

    def test_does_not_sell_at_recovery_cancel(self):
        intent = evaluate_held_bag(
            0.85, 0.86, now_s=20.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=True,
        )
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.reason, "recovery_cancel")

    def test_keep_retrying_stops_after_recovery(self):
        self.assertTrue(hedge_should_keep_retrying(3.2, 0.78, persist_done=True))
        self.assertFalse(hedge_should_keep_retrying(3.2, 0.99, persist_done=True))
        self.assertFalse(hedge_should_keep_retrying(3.2, 0.85, persist_done=True))
        self.assertTrue(hedge_should_keep_retrying(3.2, 0.50, persist_done=True))


if __name__ == "__main__":
    unittest.main()
