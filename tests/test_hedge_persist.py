"""Unit tests for the 5m persist hedge gate (no network)."""

from __future__ import annotations

import unittest
from pathlib import Path

from buy.hedge_gate import (
    clob_min_tick_from_error,
    evaluate_held_bag,
    hedge_dump_overrides_oracle,
    hedge_market_tick,
    hedge_oracle_allows_sell,
    hedge_oracle_blocks_sell,
    hedge_persist_ready,
    hedge_rest_required,
    hedge_should_keep_retrying,
    hedge_tick_after_build_error,
    held_hedge_decision,
    should_log_hedge_fill_on_uncertain,
    should_mark_hedge_closed,
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


class HourlyHeldBag50Tests(unittest.TestCase):
    """Hourly persist 5s @ 50/52, dump ≤35, recovery ≥53, fade sells."""

    KW = dict(
        dump_bid_max=0.35,
        qualify_bid=0.50,
        qualify_ask_max=0.52,
        recovery_cancel=0.53,
        persist_s=5.0,
        sell_fade=True,
    )

    def test_persist_done_bid_050_sells_live_bid(self):
        intent = evaluate_held_bag(
            0.50, 0.52, now_s=20.0, persist_armed_ts=10.0, persist_done=True,
            **self.KW,
        )
        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.reason, "persist_live_bid")
        self.assertAlmostEqual(intent.sell_at, 0.50)

    def test_persist_done_bid_051_sells_not_the_70_84_window(self):
        intent = evaluate_held_bag(
            0.51, 0.52, now_s=20.0, persist_armed_ts=10.0, persist_done=True,
            **self.KW,
        )
        self.assertEqual(intent.action, "sell")
        self.assertAlmostEqual(intent.sell_at, 0.51)

    def test_bid_055_after_persist_is_recovery(self):
        intent = evaluate_held_bag(
            0.55, 0.56, now_s=20.0, persist_armed_ts=10.0, persist_done=True,
            **self.KW,
        )
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.reason, "recovery_cancel")
        self.assertFalse(intent.persist_done)

    def test_bid_035_dumps(self):
        intent = evaluate_held_bag(
            0.35, 0.90, now_s=20.0, persist_armed_ts=None, persist_done=False,
            gui_ok=False, **self.KW,
        )
        self.assertEqual(intent.action, "dump")
        self.assertTrue(intent.dump)
        self.assertTrue(intent.skip_gui)

    def test_fade_through_50_after_persist_still_sells(self):
        intent = evaluate_held_bag(
            0.40, 0.42, now_s=20.0, persist_armed_ts=10.0, persist_done=True,
            **self.KW,
        )
        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.reason, "persist_live_bid")
        self.assertAlmostEqual(intent.sell_at, 0.40)

    def test_without_fade_040_is_dead_band(self):
        kw = dict(self.KW)
        kw["sell_fade"] = False
        intent = evaluate_held_bag(
            0.40, 0.42, now_s=20.0, persist_armed_ts=10.0, persist_done=True,
            **kw,
        )
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.reason, "dead_band")

    def test_bid_075_after_persist_is_recovery(self):
        intent = evaluate_held_bag(
            0.75, 0.76, now_s=20.0, persist_armed_ts=10.0, persist_done=True,
            **self.KW,
        )
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.reason, "recovery_cancel")
        self.assertFalse(intent.persist_done)

    def test_arm_50_52_then_wait_5s(self):
        armed = evaluate_held_bag(
            0.50, 0.52, now_s=10.0, persist_armed_ts=None, persist_done=False,
            gui_ok=True, **self.KW,
        )
        self.assertEqual(armed.action, "arm")
        waiting = evaluate_held_bag(
            0.50, 0.52, now_s=14.5, persist_armed_ts=10.0, persist_done=False,
            gui_ok=True, **self.KW,
        )
        self.assertEqual(waiting.action, "wait")
        ready = evaluate_held_bag(
            0.50, 0.52, now_s=15.0, persist_armed_ts=10.0, persist_done=False,
            gui_ok=True, **self.KW,
        )
        self.assertEqual(ready.action, "sell")
        self.assertEqual(ready.reason, "persist_live_bid")

    def test_keep_retrying_fade_but_not_recovered(self):
        self.assertTrue(
            hedge_should_keep_retrying(
                3.2, 0.40, persist_done=True,
                dump_bid_max=0.35, qualify_bid=0.50, recovery_cancel=0.53,
                sell_fade=True,
            )
        )
        self.assertFalse(
            hedge_should_keep_retrying(
                3.2, 0.55, persist_done=True,
                dump_bid_max=0.35, qualify_bid=0.50, recovery_cancel=0.53,
                sell_fade=True,
            )
        )
        self.assertFalse(
            hedge_should_keep_retrying(
                3.2, 0.40, persist_done=True,
                dump_bid_max=0.35, qualify_bid=0.50, recovery_cancel=0.53,
                sell_fade=False,
            )
        )


class HedgeOracleAllowsSellTests(unittest.TestCase):
    """Once holding, live BTC vs PTB vetoes CLOB-only false hedges."""

    def test_still_winning_does_not_sell(self):
        allow, why = hedge_oracle_allows_sell(
            "up", {"ok": True, "favored": "up", "edge_usd": 12.0},
        )
        self.assertFalse(allow)
        self.assertEqual(why, "oracle_still_winning")
        allow, why = hedge_oracle_allows_sell(
            "down", {"ok": True, "favored": "down", "edge_usd": -4.0},
        )
        self.assertFalse(allow)
        self.assertEqual(why, "oracle_still_winning")

    def test_flipped_oracle_allows_book_hedge(self):
        allow, why = hedge_oracle_allows_sell(
            "up", {"ok": True, "favored": "down", "edge_usd": -8.0},
        )
        self.assertTrue(allow)
        self.assertEqual(why, "oracle_against")

    def test_flat_allows_book_hedge(self):
        allow, why = hedge_oracle_allows_sell(
            "up", {"ok": False, "favored": None, "reason": "edge_zero"},
        )
        self.assertTrue(allow)
        self.assertEqual(why, "oracle_flat")

    def test_missing_or_stale_holds(self):
        allow, why = hedge_oracle_allows_sell(
            "up", {"ok": False, "favored": None, "reason": "live_stale"},
        )
        self.assertFalse(allow)
        self.assertEqual(why, "oracle_unknown")
        allow, why = hedge_oracle_allows_sell("up", None)
        self.assertFalse(allow)
        self.assertEqual(why, "oracle_unknown")

    def test_disabled_passes_through(self):
        allow, why = hedge_oracle_allows_sell(
            "up", {"ok": True, "favored": "up"}, enabled=False,
        )
        self.assertTrue(allow)
        self.assertEqual(why, "oracle_off")


class HedgeDumpOverridesOracleTests(unittest.TestCase):
    """Dump ≤32¢ is book-only; persist-50 stays behind the oracle."""

    def test_dump_bid_overrides(self):
        self.assertTrue(
            hedge_dump_overrides_oracle(0.32, 0.32, enabled=True),
        )
        self.assertTrue(
            hedge_dump_overrides_oracle(0.04, 0.32, enabled=True),
        )
        self.assertFalse(
            hedge_dump_overrides_oracle(0.50, 0.32, enabled=True),
        )

    def test_disabled_never_overrides(self):
        self.assertFalse(
            hedge_dump_overrides_oracle(0.04, 0.32, enabled=False),
        )
        self.assertFalse(hedge_dump_overrides_oracle(None, 0.32))


class HedgeRestRequiredTests(unittest.TestCase):
    """REST unless persist is already done or a fresh WS dump peek is enough."""

    def test_fetches_rest_on_a_normal_held_tick(self):
        self.assertTrue(
            hedge_rest_required(persist_done=False, peek_dump=False),
        )

    def test_skips_rest_after_persist_or_fresh_ws_dump(self):
        self.assertFalse(
            hedge_rest_required(persist_done=True, peek_dump=False),
        )
        self.assertFalse(
            hedge_rest_required(persist_done=False, peek_dump=True),
        )


class HedgeOracleBlocksSellTests(unittest.TestCase):
    """Dump never blocked when ignore-oracle is on; persist stays gated."""

    def test_dump_skips_oracle_when_ignore_on(self):
        self.assertFalse(
            hedge_oracle_blocks_sell(
                dump=True, oracle_agrees=True, dump_ignore_oracle=True,
            )
        )

    def test_persist_sell_blocked_while_oracle_agrees(self):
        self.assertTrue(
            hedge_oracle_blocks_sell(
                dump=False, oracle_agrees=True, dump_ignore_oracle=True,
            )
        )
        self.assertFalse(
            hedge_oracle_blocks_sell(
                dump=False, oracle_agrees=False, dump_ignore_oracle=True,
            )
        )

    def test_dump_stays_gated_when_ignore_off(self):
        self.assertTrue(
            hedge_oracle_blocks_sell(
                dump=True, oracle_agrees=True, dump_ignore_oracle=False,
            )
        )


class HeldHedgeCompositionTests(unittest.TestCase):
    """REST + evaluate before oracle. Last-good dump must not skip REST."""

    KW = dict(
        now_s=20.0,
        persist_armed_ts=10.0,
        persist_s=5.0,
        dump_bid_max=0.32,
        qualify_bid=0.50,
        qualify_ask_max=0.52,
        recovery_cancel=0.53,
        sell_fade=True,
        dump_ignore_oracle=True,
    )

    def test_oracle_agrees_no_cache_rest_32_dumps(self):
        intent = held_hedge_decision(
            0.32, 0.90, None, None, None, None,
            persist_done=False, oracle_agrees=True, **self.KW,
        )
        self.assertEqual(intent.action, "dump")
        self.assertTrue(intent.dump)

    def test_oracle_agrees_at_50_52_does_not_persist_sell(self):
        intent = held_hedge_decision(
            0.50, 0.52, None, None, None, None,
            persist_done=True, oracle_agrees=True, **self.KW,
        )
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.reason, "oracle_still_winning")
        self.assertNotEqual(intent.action, "sell")
        self.assertFalse(intent.dump)

    def test_ws_80_last_good_32_does_not_dump(self):
        intent = held_hedge_decision(
            None, None, 0.80, 0.82, 0.32, 0.90,
            persist_done=False, oracle_agrees=True, **self.KW,
        )
        self.assertNotEqual(intent.action, "dump")
        self.assertFalse(intent.dump)

    def test_stale_last_good_32_still_fetches_rest_80(self):
        """last-good 32 must not skip REST; a live 80¢ book is not a dump."""
        intent = held_hedge_decision(
            0.80, 0.82, None, None, 0.32, 0.90,
            persist_done=False, oracle_agrees=True, **self.KW,
        )
        self.assertNotEqual(intent.action, "dump")
        self.assertFalse(intent.dump)


class HedgeFillOnUncertainResolvedTests(unittest.TestCase):
    """27–31 Aug last-120: 0 hedge_fill, 52 uncertain_resolved, 52 CSV sells."""

    def test_gate_matches_hedge_closed(self):
        self.assertTrue(should_log_hedge_fill_on_uncertain(3.2, 0.0))
        self.assertTrue(should_mark_hedge_closed(3.2, 0.0))
        self.assertFalse(should_log_hedge_fill_on_uncertain(0.0, 3.2))
        self.assertFalse(should_log_hedge_fill_on_uncertain(1.0, 2.0))
        self.assertFalse(should_log_hedge_fill_on_uncertain(None, 0.0))

    def test_5m_logs_hedge_fill_after_uncertain_resolved(self):
        src = (
            Path(__file__).resolve().parents[1] / "buybot5m.py"
        ).read_text()
        resolved = src.find('"hedge_uncertain_resolved"')
        fill = src.find('"hedge_fill"', resolved)
        via = src.find('via="uncertain_resolved"', fill)
        self.assertGreater(resolved, 0)
        self.assertGreater(fill, resolved)
        self.assertGreater(via, fill)
        self.assertIn("should_log_hedge_fill_on_uncertain(", src)
        hourly = (
            Path(__file__).resolve().parents[1] / "buybothourly.py"
        ).read_text()
        self.assertNotIn("should_log_hedge_fill_on_uncertain", hourly)


if __name__ == "__main__":
    unittest.main()
