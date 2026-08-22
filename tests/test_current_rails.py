"""CURRENT 5m rails — today's live bugs must fail before the fix and pass after.

22 Aug 2026 (Europe/Dublin): 09:35 / 11:20 / 11:25 bags expired instead of
dumping. These cases are the spec. No 75/50 or 65/50 invention.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from buy.entry_skip import (
    classify_fill_against_band,
    decide_5m_entry,
    stamp_slice_on_inventory,
)
from buy.hedge_gate import (
    evaluate_held_bag,
    hedge_fail_is_terminal,
    hedge_market_tick,
    hedge_should_keep_retrying,
    live_bag_log_fields,
    pick_held_quote,
    should_mark_hedge_closed,
)
from buy.market import (
    FIVE_M_DURATION_S,
    entry_seconds_left,
    slug_window_end_ts,
)
try:
    from tests.test_buy_fill_shapes import BOT5M, _load_funcs
except ImportError:
    from test_buy_fill_shapes import BOT5M, _load_funcs


def _band(ttm, ask):
    return decide_5m_entry(ttm, ask)


class LateEarlyPostGates(unittest.TestCase):
    """Required: late 93 @ 116 no POST; early 85 @ 180 no POST; late 80 @ 41 POST."""

    def test_late_ttm_116_ask_93_no_post(self):
        self.assertIsNone(_band(116, 0.93))

    def test_early_ttm_180_ask_85_no_post(self):
        self.assertIsNone(_band(180, 0.85))

    def test_late_ttm_41_ask_80_posts_90_limit_under_3(self):
        band = _band(41, 0.80)
        self.assertIsNotNone(band)
        self.assertEqual(band.name, "late")
        self.assertAlmostEqual(band.max_price, 0.90)
        self.assertAlmostEqual(band.fak_limit, 0.90)
        ns = _load_funcs(
            "finite_float",
            "quoted_buy_shares",
            "quoted_buy_shares_up_to_limit",
            bot=BOT5M,
        )
        shares = ns["quoted_buy_shares_up_to_limit"](
            2.50, 0.80, 0.90, 5.0, spend_cap=3.0,
        )
        self.assertGreaterEqual(shares, 3.0)
        self.assertLessEqual(shares * 0.90, 3.0 + 1e-9)

    def test_gamma_end_later_than_slug_cannot_keep_early_99(self):
        slug = "btc-updown-5m-1780000000"
        slug_end = slug_window_end_ts(slug, FIVE_M_DURATION_S)
        self.assertEqual(slug_end, 1780000000 + 300)
        gamma_end = slug_end + 7.0
        now = slug_end - 116.0
        ttm = entry_seconds_left(now, gamma_end, slug, FIVE_M_DURATION_S)
        self.assertAlmostEqual(ttm, 116.0)
        self.assertIsNone(_band(ttm, 0.93))


class FillOutsideBandIsToxic(unittest.TestCase):
    def test_late_87_in_band_is_not_toxic(self):
        below, above, toxic = classify_fill_against_band(0.87, 0.75, 0.90, 0.65)
        self.assertFalse(below)
        self.assertFalse(above)
        self.assertFalse(toxic)

    def test_late_93_fill_is_toxic(self):
        _below, above, toxic = classify_fill_against_band(0.93, 0.75, 0.90, 0.65)
        self.assertTrue(above)
        self.assertTrue(toxic)

    def test_early_85_fill_is_toxic(self):
        below, _above, toxic = classify_fill_against_band(0.85, 0.90, 0.99, 0.65)
        self.assertTrue(below)
        self.assertTrue(toxic)

    def test_ghost_timeout_does_not_consume_slice(self):
        meta = {}
        self.assertFalse(stamp_slice_on_inventory(meta, True, 0.0))
        self.assertFalse(meta.get("late_bought"))
        self.assertTrue(stamp_slice_on_inventory(meta, True, 3.2))
        self.assertTrue(meta["late_bought"])
        self.assertFalse(should_mark_hedge_closed(0.0, 3.2))
        self.assertTrue(should_mark_hedge_closed(3.2, 0.0))


class HeldBagDumpAndDeadBand(unittest.TestCase):
    def test_persist_done_bid_047_dumps_at_live_bid(self):
        intent = evaluate_held_bag(
            0.47, 0.55, now_s=12.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=True, gui_ok=False, gui_why="last_trade_too_high",
        )
        self.assertEqual(intent.action, "dump")
        self.assertTrue(intent.dump)
        self.assertTrue(intent.skip_gui)
        self.assertAlmostEqual(intent.sell_at, 0.47)
        self.assertFalse(
            hedge_fail_is_terminal("empty", 3.2, 0.47, persist_done=True)
        )

    def test_bid_003_dumps_no_gui_veto(self):
        intent = evaluate_held_bag(
            0.03, 0.04, now_s=1.0, persist_armed_ts=None, persist_s=2.0,
            gui_ok=False, gui_why="incomplete_gui",
        )
        self.assertEqual(intent.action, "dump")
        self.assertTrue(intent.skip_gui)
        self.assertAlmostEqual(intent.sell_at, 0.03)

    def test_wide_022_077_dumps_on_bid(self):
        intent = evaluate_held_bag(
            0.22, 0.77, now_s=1.0, persist_armed_ts=None, persist_s=2.0,
            gui_ok=False,
        )
        self.assertEqual(intent.action, "dump")
        self.assertAlmostEqual(intent.sell_at, 0.22)

    def test_persist_done_bid_074_allows_sell(self):
        intent = evaluate_held_bag(
            0.74, 0.76, now_s=20.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=True,
        )
        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.reason, "persist_live_bid")
        self.assertAlmostEqual(intent.sell_at, 0.74)
        self.assertIsNone(intent.abort_above)

    def test_bid_061_persist_not_done_does_not_sell(self):
        intent = evaluate_held_bag(
            0.61, 0.70, now_s=10.5, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=False, gui_ok=True,
        )
        self.assertNotEqual(intent.action, "dump")
        self.assertNotEqual(intent.action, "sell")
        self.assertIn(intent.action, {"arm", "wait", "hold"})

    def test_dead_band_after_persist_does_not_sell_061(self):
        intent = evaluate_held_bag(
            0.61, 0.70, now_s=13.0, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=True,
        )
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.reason, "dead_band")

    def test_incomplete_rest_uses_ws_then_last_good(self):
        bid, ask = pick_held_quote(None, None, 0.22, 0.77, 0.25, 0.30)
        self.assertAlmostEqual(bid, 0.22)
        self.assertAlmostEqual(ask, 0.77)
        bid, ask = pick_held_quote(None, None, None, None, 0.47, 0.55)
        self.assertAlmostEqual(bid, 0.47)
        self.assertAlmostEqual(ask, 0.55)

    def test_json_tick_001_market_01_uses_01(self):
        self.assertEqual(hedge_market_tick("0.01", "0.001"), 0.01)
        self.assertEqual(hedge_market_tick(0.01, 0.001), 0.01)

    def test_live_bag_log_fields_include_required_keys(self):
        fields = live_bag_log_fields(
            slug="btc-updown-5m-1", ttm=48.2, bid=0.25, ask=0.40,
            tick=0.01, reason="bid_le_dump", order_error="no orders found",
        )
        for key in ("slug", "ttm", "bid", "ask", "tick", "reason", "order_error"):
            self.assertIn(key, fields)


class SellExecutionRails(unittest.TestCase):
    """Extracted 5m sell path: dump retries, 74¢ after persist, 0.01 tick."""

    @staticmethod
    def _sell_ns(post_error=None, quotes=None, confirmed=0.0):
        from buy.hedge_gate import (
            hedge_market_tick,
            hedge_tick_after_build_error,
            hedge_should_keep_retrying,
        )

        ns = _load_funcs(
            "hedge_exec_tick",
            "hedge_sell_price",
            "_result_as_dict",
            "sell_market_with_retry",
            bot=BOT5M,
        )
        calls = {"post": 0, "submit": [], "orders": [], "options": []}
        signed_order = SimpleNamespace(
            makerAmount="3200000",
            takerAmount="1504000",
            timestamp="1",
        )
        quote_list = list(quotes or [(0.47, 0.55)])

        def get_quote(*_a, **_k):
            if quote_list:
                bid, ask = quote_list[0]
                if len(quote_list) > 1:
                    quote_list.pop(0)
                return bid, 10.0, ask, 10.0, None
            return None, 0.0, None, 0.0, None

        def post(*_a, **_k):
            calls["post"] += 1
            if post_error:
                raise post_error
            return {"status": "matched", "orderID": "order-sell"}

        def create_market_order(args, **kwargs):
            calls["orders"].append(args)
            return signed_order

        def create_options(**kwargs):
            calls["options"].append(kwargs)
            return kwargs

        ns.update(
            {
                "DRY_RUN": False,
                "SELL": "SELL",
                "EXPECTED_TICK_SIZE": "0.001",
                "TICK_SIZE_FALLBACK": "0.001",
                "hedge_market_tick": hedge_market_tick,
                "hedge_tick_after_build_error": hedge_tick_after_build_error,
                "hedge_should_keep_retrying": hedge_should_keep_retrying,
                "console": SimpleNamespace(print=lambda *_a, **_k: None),
                "log_event": lambda *_a, **_k: None,
                "get_quote_fast": get_quote,
                "hedge_book_ok": lambda *_a, **_k: (True, "ok"),
                "safe_api_call": lambda fn, *a, **k: fn(*a, **k),
                "client": SimpleNamespace(
                    create_market_order=create_market_order,
                    post_order=post,
                ),
                "MarketOrderArgs": lambda **kwargs: kwargs,
                "PartialCreateOrderOptions": create_options,
                "OrderType": SimpleNamespace(FAK="FAK"),
                "signed_order_id": lambda *_a, **_k: "order-sell",
                "unmatched_fak_rejection": lambda exc: "no orders found to match" in str(exc).lower(),
                "definitive_order_rejection": lambda _exc: False,
                "check_clob_token_balance": lambda *_a, **_k: 3.2,
                "extract_order_id": lambda _result: "order-sell",
                "confirm_fill_size": lambda *_a, **_k: confirmed,
                "fill_proceeds": lambda *_a, **_k: confirmed * 0.47,
                "time": SimpleNamespace(sleep=lambda _s: None),
            }
        )
        return ns, calls

    def test_047_persist_done_unmatched_retries_never_idle(self):
        class Unmatched(Exception):
            status_code = 400

            def __str__(self):
                return "no orders found to match with FAK order"

        ns, calls = self._sell_ns(post_error=Unmatched(), quotes=[(0.47, 0.55)])
        sold, result, _proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.47,
            tick_size="0.001",
            max_retries=5,
            min_price=0.01,
            undercut_ticks=0,
            dump=True,
            persist_done=True,
            initial_quote=(0.47, 0.55),
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual((sold, result.get("bot_status")), (0.0, "empty"))
        self.assertGreaterEqual(calls["post"], 2)
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.47)
        self.assertFalse(
            hedge_fail_is_terminal(
                result.get("bot_status"), 3.2, 0.47, persist_done=True,
            )
        )
        self.assertTrue(
            hedge_should_keep_retrying(3.2, 0.47, persist_done=True)
        )

    def test_persist_done_074_posts_live_bid(self):
        ns, calls = self._sell_ns(quotes=[(0.74, 0.76)], confirmed=3.2)
        ns["fill_proceeds"] = lambda *_a, **_k: 3.2 * 0.74
        sold, result, _proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.74,
            tick_size="0.001",
            max_retries=1,
            persist_done=True,
            abort_above=None,
            require_ask_max=None,
            initial_quote=(0.74, 0.76),
        )
        self.assertEqual(result.get("bot_status"), "filled")
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.74)
        self.assertAlmostEqual(sold, 3.2)

    def test_incomplete_rest_uses_last_good_not_idle(self):
        class Unmatched(Exception):
            status_code = 400

            def __str__(self):
                return "no orders found to match with FAK order"

        ns, calls = self._sell_ns(post_error=Unmatched(), quotes=[(None, None)])
        sold, result, _proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.47,
            tick_size="0.01",
            max_retries=3,
            dump=True,
            refresh_quote=True,
            initial_quote=(0.47, 0.55),
        )
        self.assertGreaterEqual(calls["post"], 2)
        self.assertEqual(result.get("bot_status"), "empty")
        self.assertFalse(hedge_fail_is_terminal("empty", 3.2, 0.47, persist_done=True))

    def test_signed_sell_uses_market_tick_01(self):
        ns, calls = self._sell_ns(quotes=[(0.47, 0.55)], confirmed=3.2)
        ns["sell_market_with_retry"](
            "token", 3.2, 0.47,
            tick_size="0.001",
            market_tick="0.01",
            max_retries=1,
            dump=True,
            initial_quote=(0.47, 0.55),
        )
        self.assertEqual(calls["options"][0]["tick_size"], "0.01")
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.47)

    def test_061_not_sold_when_persist_not_done(self):
        intent = evaluate_held_bag(
            0.61, 0.70, now_s=10.4, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=False,
        )
        self.assertNotEqual(intent.action, "sell")
        self.assertNotEqual(intent.action, "dump")


class BotWiresCurrentRails(unittest.TestCase):
    def test_5m_uses_slug_clock_and_dump_helpers(self):
        src = BOT5M.read_text()
        self.assertIn("entry_seconds_left(", src)
        self.assertIn("evaluate_held_bag(", src)
        self.assertIn("pick_held_quote(", src)
        self.assertIn("stamp_slice_on_inventory(", src)
        self.assertIn("hedge_fail_is_terminal(", src)
        self.assertIn("dump=dump", src)
        self.assertIn("persist_done=", src)
        self.assertIn("market_tick=", src)
        self.assertIn("max_retries=12 if dump", src)
        self.assertNotIn(
            "seconds_left = (end_ts_ms - now_ms) / 1000",
            src,
        )


if __name__ == "__main__":
    unittest.main()
