"""Focused hourly live-money safety regressions (no bot import or network)."""

from __future__ import annotations

import ast
import unittest
from types import SimpleNamespace

from buy.entry_skip import (
    applicable_hourly_entry_bands,
    can_arm_hourly_slice,
    hourly_entry_final_gate,
)
from buy.hedge_gate import (
    evaluate_held_bag,
    hedge_should_keep_retrying,
)

try:
    from tests.test_buy_fill_shapes import BOT_HR, _load_funcs
except ImportError:
    from test_buy_fill_shapes import BOT_HR, _load_funcs


class HourlyPersistenceContinuityTests(unittest.TestCase):
    KW = dict(
        persist_s=5.0,
        dump_bid_max=0.35,
        qualify_bid=0.50,
        qualify_ask_max=0.52,
        recovery_cancel=0.53,
        sell_fade=True,
    )

    def test_elapsed_endpoint_requires_current_book(self):
        intent = evaluate_held_bag(
            0.50, 0.99,
            now_s=15.0,
            persist_armed_ts=10.0,
            persist_done=False,
            gui_ok=True,
            **self.KW,
        )
        self.assertEqual((intent.action, intent.reason), ("hold", "ask_too_high"))
        self.assertFalse(intent.persist_done)
        self.assertIsNone(intent.persist_ts)

    def test_elapsed_endpoint_requires_current_gui(self):
        intent = evaluate_held_bag(
            0.50, 0.52,
            now_s=15.0,
            persist_armed_ts=10.0,
            persist_done=False,
            gui_ok=False,
            gui_why="other_gui_too_low",
            **self.KW,
        )
        self.assertEqual(
            (intent.action, intent.reason),
            ("hold", "other_gui_too_low"),
        )
        self.assertFalse(intent.persist_done)
        self.assertIsNone(intent.persist_ts)

    def test_qualifying_endpoint_completes_and_fade_sells(self):
        ready = evaluate_held_bag(
            0.50, 0.52,
            now_s=15.0,
            persist_armed_ts=10.0,
            persist_done=False,
            gui_ok=True,
            **self.KW,
        )
        self.assertEqual((ready.action, ready.reason), ("sell", "persist_live_bid"))
        faded = evaluate_held_bag(
            0.40, 0.42,
            now_s=15.0,
            persist_armed_ts=10.0,
            persist_done=False,
            gui_ok=True,
            **self.KW,
        )
        self.assertEqual((faded.action, faded.reason), ("sell", "persist_live_bid"))

    def test_already_completed_persistence_retains_bid_only_fade(self):
        intent = evaluate_held_bag(
            0.40, 0.99,
            now_s=20.0,
            persist_armed_ts=10.0,
            persist_done=True,
            gui_ok=False,
            **self.KW,
        )
        self.assertEqual((intent.action, intent.reason), ("sell", "persist_live_bid"))
        self.assertTrue(intent.persist_done)


class HourlyEntryFinalGateTests(unittest.TestCase):
    def setUp(self):
        self.bands = applicable_hourly_entry_bands(
            10.0,
            a22_window_min=0.0,
            b15_window_min=20.0,
            c5_window_min=0.0,
        )
        self.base = dict(
            selected_slice="b15",
            buy_ask=0.85,
            bands=self.bands,
            buy_leg="up",
            oracle_check={"ok": True, "favored": "up"},
            hedge_closed=False,
            buy_book_ok=True,
            buy_clob_winner=True,
            buy_gui=0.80,
            other_gui=0.20,
            min_winner_bid=0.70,
            max_loser_bid=0.30,
            min_gui_edge=0.05,
        )

    def test_accepts_only_when_all_final_gates_still_hold(self):
        self.assertEqual(hourly_entry_final_gate(10.0, **self.base), (True, "ok"))

    def test_rejects_expiry_band_close_or_closed_market(self):
        self.assertEqual(
            hourly_entry_final_gate(0.0, **self.base),
            (False, "expired"),
        )
        changed = dict(self.base, bands=[])
        self.assertEqual(
            hourly_entry_final_gate(10.0, **changed),
            (False, "band_closed"),
        )
        changed = dict(self.base, hedge_closed=True)
        self.assertEqual(
            hourly_entry_final_gate(10.0, **changed),
            (False, "hedge_closed"),
        )

    def test_rejects_oracle_stale_or_side_flip(self):
        changed = dict(self.base, oracle_gate_enabled=False)
        self.assertEqual(
            hourly_entry_final_gate(10.0, **changed),
            (False, "underlying_gate_disabled"),
        )
        changed = dict(
            self.base,
            oracle_check={"ok": False, "reason": "live_stale"},
        )
        self.assertEqual(
            hourly_entry_final_gate(10.0, **changed),
            (False, "oracle_unavailable"),
        )
        changed = dict(
            self.base,
            oracle_check={"ok": True, "favored": "down"},
        )
        self.assertEqual(
            hourly_entry_final_gate(10.0, **changed),
            (False, "oracle_side_flip"),
        )

    def test_rejects_clob_or_gui_side_flip(self):
        changed = dict(self.base, buy_clob_winner=False)
        self.assertEqual(
            hourly_entry_final_gate(10.0, **changed),
            (False, "clob_side_flip"),
        )
        changed = dict(self.base, buy_gui=0.20, other_gui=0.80)
        self.assertEqual(
            hourly_entry_final_gate(10.0, **changed),
            (False, "clob_gui_side_flip"),
        )

    def test_hedge_closed_wins_for_every_legacy_slice(self):
        legacy = {
            "hedge_closed": True,
            "buy_uncertain": True,
            "pnl_entry_cost": 10.0,
            "t22_bought": True,
            "t15_bought": True,
            "t5_bought": True,
        }
        for slice_name in ("a22", "b15", "c5"):
            with self.subTest(slice=slice_name):
                self.assertEqual(
                    can_arm_hourly_slice(
                        legacy,
                        slice_name=slice_name,
                        held_size=0.0,
                    ),
                    (False, "hedge_closed"),
                )


class HourlyHedgeGuiBoundaryTests(unittest.TestCase):
    def test_documented_50_vs_48_boundary_ignores_buy_gap(self):
        ns = _load_funcs(
            "finite_float",
            "polymarket_display_price",
            "hedge_consensus_ok",
            bot=BOT_HR,
        )
        ok, why = ns["hedge_consensus_ok"](
            0.49, 0.51, 0.50,
            0.47, 0.49, 0.48,
            held_gui_max=0.52,
            other_gui_min=0.48,
            min_edge=0.05,
            last_trade_max=0.52,
        )
        self.assertTrue(ok)
        self.assertEqual(why, "ok")


class HourlyExecutorSafetyTests(unittest.TestCase):
    class Unmatched(Exception):
        status_code = 400

        def __str__(self):
            return "no orders found to match with FAK order"

    @staticmethod
    def _buy_namespace():
        ns = _load_funcs(
            "finite_float",
            "_result_as_dict",
            "quoted_buy_shares",
            "quoted_buy_shares_up_to_limit",
            "buy_fill_walked",
            "classify_buy_fill",
            "implied_buy_average",
            "buy_market_with_retry",
            "unmatched_fak_rejection",
            "definitive_order_rejection",
            bot=BOT_HR,
        )
        calls = {
            "post": 0,
            "hooks": 0,
            "clock": 90.0,
            "abort": 0,
        }
        signed = SimpleNamespace(
            makerAmount="9990000",
            takerAmount="11100000",
            timestamp="1",
        )

        def post(*_args, **_kwargs):
            calls["post"] += 1
            raise HourlyExecutorSafetyTests.Unmatched()

        ns.update(
            {
                "DRY_RUN": False,
                "BUY": "BUY",
                "BUY_MAX_SHARES": 14.0,
                "BUY_MAX_SPEND": 11.0,
                "HEDGE_GHOST_SLEEP_S": 0.0,
                "MAX_ENTRY_SPREAD": 0.10,
                "MIN_WINNER_BID": 0.70,
                "TOXIC_FORCE_EXIT_BELOW": 0.65,
                "console": SimpleNamespace(print=lambda *_a, **_k: None),
                "log_event": lambda *_a, **_k: None,
                "check_token_balance": lambda *_a, **_k: 0.0,
                "check_clob_token_balance": lambda *_a, **_k: 0.0,
                "_fill_fee_usdc": lambda *_a, **_k: None,
                "get_quote_fast": lambda *_a, **_k: (
                    0.84, 10.0, 0.85, 10.0, 0.845,
                ),
                "entry_book_ok": lambda *_a, **_k: (True, "ok"),
                "safe_api_call": lambda fn, *a, **k: fn(*a, **k),
                "client": SimpleNamespace(
                    create_order=lambda *_a, **_k: signed,
                    post_order=post,
                ),
                "OrderArgs": lambda **kwargs: kwargs,
                "PartialCreateOrderOptions": lambda **kwargs: kwargs,
                "OrderType": SimpleNamespace(FAK="FAK"),
                "signed_order_id": lambda *_a, **_k: "buy-order",
                "extract_order_id": lambda _result: "buy-order",
                "confirm_fill_size": lambda *_a, **_k: 0.0,
                "fill_cost_usdc": lambda *_a, **_k: 0.0,
                "time": SimpleNamespace(
                    time=lambda: calls["clock"],
                    sleep=lambda _s: None,
                ),
            }
        )
        return ns, calls

    @staticmethod
    def _sell_namespace(*, sell_fade=True):
        ns = _load_funcs(
            "_result_as_dict",
            "sell_market_with_retry",
            bot=BOT_HR,
        )
        calls = {
            "post": 0,
            "hooks": 0,
            "clock": 90.0,
            "abort": 0,
            "rest": 0,
            "sell_price": None,
        }
        signed = SimpleNamespace(
            makerAmount="3200000",
            takerAmount="1280000",
            timestamp="1",
        )

        def post(*_args, **_kwargs):
            calls["post"] += 1
            raise HourlyExecutorSafetyTests.Unmatched()

        def quote(*_args, **_kwargs):
            calls["rest"] += 1
            return 0.40, 10.0, 0.42, 10.0, 0.41

        def create_market_order(order_args, **_kwargs):
            calls["sell_price"] = order_args["price"]
            return signed

        ns.update(
            {
                "DRY_RUN": False,
                "SELL": "SELL",
                "HEDGE_TOXIC_BID_MAX": 0.35,
                "HEDGE_THRESHOLD": 0.50,
                "HEDGE_RECOVERY_CANCEL": 0.53,
                "HEDGE_SELL_FADE": sell_fade,
                "console": SimpleNamespace(print=lambda *_a, **_k: None),
                "log_event": lambda *_a, **_k: None,
                "hedge_exec_tick": lambda tick: float(tick or 0.01),
                "hedge_tick_after_build_error": lambda *_a, **_k: None,
                "hedge_should_keep_retrying": hedge_should_keep_retrying,
                "hedge_sell_price": lambda bid, *_a, **_k: float(bid),
                "get_quote_fast": quote,
                "hedge_book_ok": lambda *_a, **_k: (True, "ok"),
                "safe_api_call": lambda fn, *a, **k: fn(*a, **k),
                "client": SimpleNamespace(
                    create_market_order=create_market_order,
                    post_order=post,
                ),
                "MarketOrderArgs": lambda **kwargs: kwargs,
                "PartialCreateOrderOptions": lambda **kwargs: kwargs,
                "OrderType": SimpleNamespace(FAK="FAK"),
                "signed_order_id": lambda *_a, **_k: "sell-order",
                "unmatched_fak_rejection": lambda exc: isinstance(
                    exc, HourlyExecutorSafetyTests.Unmatched,
                ),
                "definitive_order_rejection": lambda _exc: False,
                "check_clob_token_balance": lambda *_a, **_k: 3.2,
                "extract_order_id": lambda _result: "sell-order",
                "confirm_fill_size": lambda *_a, **_k: 0.0,
                "fill_proceeds": lambda *_a, **_k: 0.0,
                "time": SimpleNamespace(
                    time=lambda: calls["clock"],
                    sleep=lambda _s: None,
                ),
            }
        )
        return ns, calls

    def test_hourly_fade_executor_posts_in_35_50_zone(self):
        ns, calls = self._sell_namespace(sell_fade=True)
        ns["sell_market_with_retry"](
            "token",
            3.2,
            0.40,
            persist_done=True,
            initial_quote=(0.40, 0.42),
            max_retries=1,
        )
        self.assertEqual(calls["post"], 1)

        ns, calls = self._sell_namespace(sell_fade=False)
        ns["sell_market_with_retry"](
            "token",
            3.2,
            0.40,
            persist_done=True,
            initial_quote=(0.40, 0.42),
            max_retries=1,
        )
        self.assertEqual(calls["post"], 0)

    def test_sell_attempt_zero_force_rests_and_uses_live_bid(self):
        ns, calls = self._sell_namespace()

        def quote(*_args, **_kwargs):
            calls["rest"] += 1
            return 0.44, 10.0, 0.46, 10.0, 0.45

        ns["get_quote_fast"] = quote
        ns["sell_market_with_retry"](
            "token",
            3.2,
            0.40,
            persist_done=True,
            initial_quote=(0.40, 0.42),
            max_retries=1,
        )
        self.assertEqual(calls["rest"], 1)
        self.assertAlmostEqual(calls["sell_price"], 0.44)
        self.assertEqual(calls["post"], 1)

    def test_sell_attempt_zero_falls_back_only_for_incomplete_rest(self):
        ns, calls = self._sell_namespace()

        def quote(*_args, **_kwargs):
            calls["rest"] += 1
            return None, None, None, None, None

        ns["get_quote_fast"] = quote
        ns["sell_market_with_retry"](
            "token",
            3.2,
            0.40,
            persist_done=True,
            initial_quote=(0.40, 0.42),
            max_retries=1,
        )
        self.assertEqual(calls["rest"], 1)
        self.assertAlmostEqual(calls["sell_price"], 0.40)
        self.assertEqual(calls["post"], 1)

    def test_buy_hook_runs_again_and_blocks_retry(self):
        ns, calls = self._buy_namespace()

        def gate(*_args):
            calls["hooks"] += 1
            return calls["hooks"] == 1, "side_flip"

        result = ns["buy_market_with_retry"](
            "token",
            10.0,
            0.90,
            min_price=0.75,
            max_retries=3,
            pre_submit=gate,
            deadline_ts=100.0,
        )
        self.assertEqual(result[2], "aborted")
        self.assertEqual((calls["post"], calls["hooks"]), (1, 2))

    def test_sell_hook_runs_again_and_blocks_retry(self):
        ns, calls = self._sell_namespace()

        def gate(*_args):
            calls["hooks"] += 1
            return calls["hooks"] == 1, "oracle_veto"

        _sold, result, _proceeds = ns["sell_market_with_retry"](
            "token",
            3.2,
            0.40,
            persist_done=True,
            initial_quote=(0.40, 0.42),
            max_retries=3,
            pre_submit=gate,
            deadline_ts=100.0,
        )
        self.assertEqual(result["bot_status"], "aborted")
        self.assertEqual((calls["post"], calls["hooks"]), (1, 2))

    def test_buy_and_sell_deadlines_block_post_at_expiry(self):
        buy_ns, buy_calls = self._buy_namespace()
        buy_calls["clock"] = 100.0
        result = buy_ns["buy_market_with_retry"](
            "token",
            10.0,
            0.90,
            min_price=0.75,
            deadline_ts=100.0,
        )
        self.assertEqual(result[2], "aborted")
        self.assertEqual(buy_calls["post"], 0)

        sell_ns, sell_calls = self._sell_namespace()
        sell_calls["clock"] = 100.0
        _sold, result, _proceeds = sell_ns["sell_market_with_retry"](
            "token",
            3.2,
            0.40,
            persist_done=True,
            initial_quote=(0.40, 0.42),
            deadline_ts=100.0,
        )
        self.assertEqual(result["bot_status"], "aborted")
        self.assertEqual(sell_calls["post"], 0)

    def test_deadline_cross_after_writeahead_clears_buy_and_sell_quarantine(self):
        buy_ns, buy_calls = self._buy_namespace()
        buy_state = {"uncertain": False}

        def buy_submit(*_args):
            buy_state["uncertain"] = True
            buy_calls["clock"] = 100.0

        def buy_abort():
            buy_state["uncertain"] = False
            buy_calls["abort"] += 1

        result = buy_ns["buy_market_with_retry"](
            "token",
            10.0,
            0.90,
            min_price=0.75,
            max_retries=1,
            on_submit=buy_submit,
            on_abort=buy_abort,
            pre_submit=lambda *_args: (True, "ok"),
            deadline_ts=100.0,
        )
        self.assertEqual(result[2], "aborted")
        self.assertEqual(buy_calls["post"], 0)
        self.assertEqual(buy_calls["abort"], 1)
        self.assertFalse(buy_state["uncertain"])

        sell_ns, sell_calls = self._sell_namespace()
        sell_state = {"uncertain": False}

        def sell_submit(*_args):
            sell_state["uncertain"] = True
            sell_calls["clock"] = 100.0

        def sell_abort():
            sell_state["uncertain"] = False
            sell_calls["abort"] += 1

        _sold, result, _proceeds = sell_ns["sell_market_with_retry"](
            "token",
            3.2,
            0.40,
            persist_done=True,
            initial_quote=(0.40, 0.42),
            max_retries=1,
            on_submit=sell_submit,
            on_abort=sell_abort,
            pre_submit=lambda *_args: (True, "ok"),
            deadline_ts=100.0,
        )
        self.assertEqual(result["bot_status"], "aborted")
        self.assertEqual(sell_calls["post"], 0)
        self.assertEqual(sell_calls["abort"], 1)
        self.assertFalse(sell_state["uncertain"])

    def test_final_validator_runs_between_writeahead_and_post(self):
        buy_ns, buy_calls = self._buy_namespace()
        buy_order = []

        def buy_post(*_args, **_kwargs):
            buy_calls["post"] += 1
            buy_order.append("post")
            raise self.Unmatched()

        buy_ns["client"].post_order = buy_post
        buy_ns["buy_market_with_retry"](
            "token",
            10.0,
            0.90,
            min_price=0.75,
            max_retries=1,
            on_submit=lambda *_args: buy_order.append("writeahead"),
            pre_submit=lambda *_args: (
                buy_order.append("final_gate") or True,
                "ok",
            ),
            deadline_ts=100.0,
        )
        self.assertEqual(buy_order, ["writeahead", "final_gate", "post"])

        sell_ns, sell_calls = self._sell_namespace()
        sell_order = []

        def sell_post(*_args, **_kwargs):
            sell_calls["post"] += 1
            sell_order.append("post")
            raise self.Unmatched()

        sell_ns["client"].post_order = sell_post
        sell_ns["sell_market_with_retry"](
            "token",
            3.2,
            0.40,
            persist_done=True,
            initial_quote=(0.40, 0.42),
            max_retries=1,
            on_submit=lambda *_args: sell_order.append("writeahead"),
            pre_submit=lambda *_args: (
                sell_order.append("final_gate") or True,
                "ok",
            ),
            deadline_ts=100.0,
        )
        self.assertEqual(sell_order, ["writeahead", "final_gate", "post"])


class HourlySafetyWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = BOT_HR.read_text()
        cls.tree = ast.parse(cls.src)

    def _calls(self, name):
        return [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        ]

    def test_production_calls_wire_hooks_and_deadlines(self):
        for name in ("buy_market_with_retry", "sell_market_with_retry"):
            calls = self._calls(name)
            self.assertEqual(len(calls), 1, name)
            keywords = {kw.arg for kw in calls[0].keywords}
            self.assertIn("pre_submit", keywords, name)
            self.assertIn("deadline_ts", keywords, name)
            self.assertIn("on_abort", keywords, name)

    def test_nested_final_gates_cover_oracle_time_side_and_closed_state(self):
        nested = {
            node.name: ast.get_source_segment(self.src, node)
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef)
            and node.name in {
                "_buy_pre_submit",
                "_hedge_pre_submit",
                "_abort_buy_submit",
                "_abort_hedge_submit",
            }
        }
        buy = nested["_buy_pre_submit"]
        for marker in (
            "m.end_ts",
            "underlying_check",
            "current_entry_bands",
            "hourly_entry_final_gate",
            'meta.get("hedge_closed")',
            "polymarket_display_price",
            "UNDERLYING_GATE_ENABLED",
        ):
            self.assertIn(marker, buy)
        hedge = nested["_hedge_pre_submit"]
        self.assertIn("m.end_ts", hedge)
        self.assertIn("hold_while_oracle_agrees", hedge)
        self.assertIn("hedge_dump_overrides_oracle", hedge)
        self.assertIn("_clear_buy_uncertain()", nested["_abort_buy_submit"])
        self.assertIn("save_json(", nested["_abort_buy_submit"])
        self.assertIn("_clear_hedge_uncertain()", nested["_abort_hedge_submit"])
        self.assertIn("save_json(", nested["_abort_hedge_submit"])

    def test_rest_confirm_reapplies_favored_and_outer_loop_does_not_precomplete(self):
        self.assertGreaterEqual(self.src.count('if favored == "up":'), 2)
        self.assertNotIn(
            "time.monotonic() - float(armed_ts)",
            self.src,
        )
        self.assertIn("buy_skip_hedge_closed", self.src)
        self.assertIn('reason="underlying_gate_disabled"', self.src)


if __name__ == "__main__":
    unittest.main()
