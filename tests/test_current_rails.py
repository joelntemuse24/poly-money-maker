"""CURRENT 5m rails — today's live bugs must fail before the fix and pass after.

22 Aug 2026 (Europe/Dublin): 09:35 / 11:20 / 11:25 bags expired instead of
dumping. These cases are the spec. 5m is back live: $2.50 two-slice, last-45s
≥90 overlay, persist 5s @ 50/52 + dump 32 ignore-oracle (hourly false-hedge
crackdown). Hourly stays last-20m 75–90 with persist 50/52 + dump 35.


22 Aug 16:08 UTC: polybuybot5m crashed twice in the 12:05 ET window —
``TypeError: log_buy_skip_throttled() got multiple values for argument 'reason'``
at the hedge persist-skip log (buybot5m.py ~4886).
"""

from __future__ import annotations

import ast
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy.entry_skip import (
    buy_retry_fak_limit,
    classify_fill_against_band,
    decide_5m_entry,
    stamp_slice_on_inventory,
    validate_late_90_start_s,
)
from buy.hedge_gate import (
    evaluate_held_bag,
    hedge_fail_is_terminal,
    hedge_market_tick,
    hedge_oracle_blocks_sell,
    hedge_rest_required,
    hedge_should_keep_retrying,
    held_hedge_decision,
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
    from tests.test_buy_fill_shapes import BOT5M, BOT_HR, _load_funcs
except ImportError:
    from test_buy_fill_shapes import BOT5M, BOT_HR, _load_funcs


def _band(ttm, ask):
    return decide_5m_entry(ttm, ask)


class LateEarlyPostGates(unittest.TestCase):
    """Required: late 93 @ 116 no POST; last 45s 93 POST at 99; late 80 @ 41 POST."""

    def test_late_ttm_116_ask_93_no_post(self):
        self.assertIsNone(_band(116, 0.93))

    def test_late_ttm_60_ask_93_no_post(self):
        self.assertIsNone(_band(60, 0.93))

    def test_late_ttm_40_ask_93_posts_99_limit(self):
        band = _band(40, 0.93)
        self.assertIsNotNone(band)
        self.assertEqual(band.name, "late_90")
        self.assertAlmostEqual(band.min_price, 0.90)
        self.assertAlmostEqual(band.max_price, 0.99)
        self.assertAlmostEqual(band.fak_limit, 0.99)
        ns = _load_funcs(
            "finite_float",
            "quoted_buy_shares",
            "quoted_buy_shares_up_to_limit",
            bot=BOT5M,
        )
        shares = ns["quoted_buy_shares_up_to_limit"](
            2.50, 0.93, 0.99, 5.0, spend_cap=3.0,
        )
        self.assertGreaterEqual(shares, 3.0)
        self.assertLessEqual(shares * 0.99, 3.0 + 1e-9)

    def test_late_ttm_40_ask_90_still_posts_late_90c_fak(self):
        band = _band(40, 0.90)
        self.assertIsNotNone(band)
        self.assertEqual(band.name, "late")
        self.assertAlmostEqual(band.fak_limit, 0.90)

    def test_post_44s_93_allows_99_46s_93_rejects_44s_90_stays_fak_90(self):
        armed = 0.99
        live_44_93 = _band(44, 0.93)
        self.assertIsNotNone(live_44_93)
        self.assertEqual(live_44_93.name, "late_90")
        self.assertAlmostEqual(buy_retry_fak_limit(armed, live_44_93), 0.99)
        self.assertIsNone(_band(46, 0.93))
        self.assertIsNone(buy_retry_fak_limit(armed, _band(46, 0.93)))
        live_44_90 = _band(44, 0.90)
        self.assertIsNotNone(live_44_90)
        self.assertEqual(live_44_90.name, "late")
        self.assertAlmostEqual(buy_retry_fak_limit(armed, live_44_90), 0.90)

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
        self.assertAlmostEqual(intent.abort_above, 0.85)

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
    """Extracted 5m sell path: dump ≤32 retries, persist fade <53, 0.01 tick."""

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
                "HEDGE_TOXIC_BID_MAX": 0.32,
                "HEDGE_THRESHOLD": 0.50,
                "HEDGE_RECOVERY_CANCEL": 0.53,
                "HEDGE_SELL_FADE": True,
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

        ns, calls = self._sell_ns(post_error=Unmatched(), quotes=[(0.31, 0.40)])
        sold, result, _proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.31,
            tick_size="0.001",
            max_retries=5,
            min_price=0.01,
            undercut_ticks=0,
            dump=True,
            persist_done=True,
            initial_quote=(0.31, 0.40),
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual((sold, result.get("bot_status")), (0.0, "empty"))
        self.assertGreaterEqual(calls["post"], 2)
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.31)
        self.assertFalse(
            hedge_fail_is_terminal(
                result.get("bot_status"), 3.2, 0.31, persist_done=True,
                dump_bid_max=0.32, qualify_bid=0.50, recovery_cancel=0.53,
                sell_fade=True,
            )
        )
        self.assertTrue(
            hedge_should_keep_retrying(
                3.2, 0.31, persist_done=True,
                dump_bid_max=0.32, qualify_bid=0.50, recovery_cancel=0.53,
                sell_fade=True,
            )
        )

    def test_persist_done_051_posts_live_bid(self):
        ns, calls = self._sell_ns(quotes=[(0.51, 0.52)], confirmed=3.2)
        ns["fill_proceeds"] = lambda *_a, **_k: 3.2 * 0.51
        sold, result, _proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.51,
            tick_size="0.001",
            max_retries=1,
            persist_done=True,
            abort_above=None,
            require_ask_max=None,
            initial_quote=(0.51, 0.52),
        )
        self.assertEqual(result.get("bot_status"), "filled")
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.51)
        self.assertAlmostEqual(sold, 3.2)

    def test_persist_done_074_aborts_without_post(self):
        ns, calls = self._sell_ns(quotes=[(0.74, 0.76)], confirmed=3.2)
        sold, result, _proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.74,
            tick_size="0.001",
            max_retries=1,
            persist_done=True,
            abort_above=None,
            require_ask_max=None,
            initial_quote=(0.74, 0.76),
        )
        self.assertEqual(calls["post"], 0)
        self.assertEqual(sold, 0.0)
        self.assertNotEqual(result.get("bot_status"), "filled")

    def test_persist_done_090_aborts_without_post(self):
        ns, calls = self._sell_ns(quotes=[(0.90, 0.92)], confirmed=3.2)
        sold, result, _proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.90,
            tick_size="0.001",
            max_retries=3,
            persist_done=True,
            abort_above=0.85,
            initial_quote=(0.90, 0.92),
        )
        self.assertEqual(calls["post"], 0)
        self.assertEqual(sold, 0.0)
        self.assertNotEqual(result.get("bot_status"), "filled")

    def test_incomplete_rest_uses_last_good_not_idle(self):
        class Unmatched(Exception):
            status_code = 400

            def __str__(self):
                return "no orders found to match with FAK order"

        ns, calls = self._sell_ns(post_error=Unmatched(), quotes=[(None, None)])
        sold, result, _proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.31,
            tick_size="0.01",
            max_retries=3,
            dump=True,
            refresh_quote=True,
            initial_quote=(0.31, 0.40),
        )
        self.assertGreaterEqual(calls["post"], 2)
        self.assertEqual(result.get("bot_status"), "empty")
        self.assertFalse(hedge_fail_is_terminal(
            "empty", 3.2, 0.31, persist_done=True,
            dump_bid_max=0.32, qualify_bid=0.50, recovery_cancel=0.53,
            sell_fade=True,
        ))

    def test_signed_sell_uses_market_tick_01(self):
        ns, calls = self._sell_ns(quotes=[(0.31, 0.40)], confirmed=3.2)
        ns["sell_market_with_retry"](
            "token", 3.2, 0.31,
            tick_size="0.001",
            market_tick="0.01",
            max_retries=1,
            dump=True,
            initial_quote=(0.31, 0.40),
        )
        self.assertEqual(calls["options"][0]["tick_size"], "0.01")
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.31)

    def test_061_not_sold_when_persist_not_done(self):
        intent = evaluate_held_bag(
            0.61, 0.70, now_s=10.4, persist_armed_ts=10.0, persist_s=2.0,
            persist_done=False,
        )
        self.assertNotEqual(intent.action, "sell")
        self.assertNotEqual(intent.action, "dump")


def _throttled_skip_calls(path: Path):
    """Every ``log_buy_skip_throttled(...)`` Call in *path* (lineno, node)."""
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else ""
        if name != "log_buy_skip_throttled":
            continue
        out.append(node)
    return out


def _dict_literal_has_key(node, key: str) -> bool:
    """True if a dict literal (including ``{**a, **{"key": ...}}``) sets *key*."""
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == key:
                return True
            if k is None and _dict_literal_has_key(v, key):
                return True
        return False
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"dict"}:
            for kw in node.keywords:
                if kw.arg == key:
                    return True
                if kw.arg is None and _dict_literal_has_key(kw.value, key):
                    return True
    return False


def _call_passes_reason_twice(node: ast.Call) -> bool:
    """Positional *reason* plus ``reason=`` / ``**{..., "reason": ...}``."""
    if len(node.args) < 1:
        return False
    for kw in node.keywords:
        if kw.arg == "reason":
            return True
        if kw.arg is None and _dict_literal_has_key(kw.value, key="reason"):
            return True
    return False


def _load_skip_logger():
    """Extract 5m skip logger + log_event (bots are not importable)."""
    recorded = []
    ns = _load_funcs("log_event", "log_buy_skip_throttled", bot=BOT5M)
    ns.update(
        {
            "time": time,
            "_SKIP_LOG_EVERY_S": 0.0,
            "_skip_log_mono": {},
            "is_journal_event": lambda _e: False,
            "_file_logger": SimpleNamespace(info=recorded.append),
            "_journal_logger": SimpleNamespace(info=lambda _line: None),
        }
    )
    return ns, recorded


class PersistSkipReasonCollision(unittest.TestCase):
    """Live 22 Aug 16:08 UTC: persist-skip logged reason twice and crashed."""

    def test_persist_skip_kwargs_with_reason_do_not_typeerror(self):
        """Construct the live persist-skip kwargs; calling the logger must not raise.

        The VM call was::

            log_buy_skip_throttled(
                intent.reason, cond, event="hedge_skip_persist",
                **{**bag_log, **{"reason": intent.reason}},
                persist_s=..., persist_why=intent.reason, threshold=...,
            )
        """
        intent = evaluate_held_bag(
            0.70, 0.72, now_s=10.0, persist_armed_ts=None, persist_s=2.0,
            persist_done=False, gui_ok=True,
        )
        self.assertEqual(intent.action, "arm")
        self.assertEqual(intent.reason, "persist_armed")
        bag_log = live_bag_log_fields(
            slug="btc-updown-5m-1780000000", ttm=48.2, bid=0.70, ask=0.72,
            tick=0.01,
        )
        self.assertNotIn("reason", bag_log)
        kwargs = {**bag_log, **{"reason": intent.reason}}
        ns, recorded = _load_skip_logger()
        ns["log_buy_skip_throttled"](
            intent.reason,
            "cond-1205et",
            event="hedge_skip_persist",
            **kwargs,
            persist_s=2.0,
            persist_why=intent.reason,
            threshold=0.70,
        )
        self.assertGreaterEqual(len(recorded), 1)
        entry = json.loads(recorded[-1])
        self.assertEqual(entry["event"], "hedge_skip_persist")
        self.assertEqual(entry["reason"], "persist_armed")
        self.assertEqual(entry["condition_id"], "cond-1205et")
        self.assertAlmostEqual(entry["bid"], 0.70)
        self.assertAlmostEqual(entry["persist_s"], 2.0)

    def test_bag_log_with_reason_does_not_collide_log_event(self):
        """``live_bag_log_fields(..., reason=)`` plus positional reason.

        ``log_event(event, reason=reason, **kwargs)`` would TypeError if
        kwargs still carried ``reason``.
        """
        bag_log = live_bag_log_fields(
            slug="btc-updown-5m-1", ttm=48.2, bid=0.61, ask=0.70,
            tick=0.01, reason="persist_waiting",
        )
        self.assertIn("reason", bag_log)
        ns, recorded = _load_skip_logger()
        ns["log_buy_skip_throttled"](
            "persist_waiting",
            "cond-wait",
            event="hedge_skip_persist",
            **bag_log,
            persist_s=2.0,
            persist_why="persist_waiting",
            threshold=0.70,
        )
        entry = json.loads(recorded[-1])
        self.assertEqual(entry["reason"], "persist_waiting")
        self.assertEqual(entry["event"], "hedge_skip_persist")

    def test_no_throttled_skip_caller_passes_reason_twice(self):
        """Scan every ``log_buy_skip_throttled`` call in 5m + hourly."""
        bots = (
            Path(__file__).resolve().parents[1] / "buybot5m.py",
            Path(__file__).resolve().parents[1] / "buybothourly.py",
        )
        collisions = []
        persist_skip_calls = 0
        for path in bots:
            for node in _throttled_skip_calls(path):
                ev = None
                for kw in node.keywords:
                    if kw.arg == "event" and isinstance(kw.value, ast.Constant):
                        ev = kw.value.value
                if ev == "hedge_skip_persist":
                    persist_skip_calls += 1
                if _call_passes_reason_twice(node):
                    collisions.append(f"{path.name}:{node.lineno}")
        self.assertGreaterEqual(
            persist_skip_calls, 1,
            "expected the 5m persist-skip log_buy_skip_throttled call",
        )
        self.assertEqual(
            collisions, [],
            "positional reason plus reason in kwargs: " + ", ".join(collisions),
        )


class FiveMBuyRetryPinRails(unittest.TestCase):
    """Armed late_90 FAK 99 must pin to the live band, not abort or walk 99."""

    @staticmethod
    def _buy_ns(ask, post_error=None):
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
            bot=BOT5M,
        )
        calls = {"post": 0, "orders": []}
        signed = SimpleNamespace(
            makerAmount="2500000",
            takerAmount="2525253",
            timestamp="1",
        )

        def post(*_a, **_k):
            calls["post"] += 1
            if post_error:
                raise post_error
            return {"status": "matched", "orderID": "order-buy"}

        def create_order(args, **_kwargs):
            calls["orders"].append(args)
            return signed

        ns.update(
            {
                "DRY_RUN": False,
                "BUY": "BUY",
                "BUY_MAX_SHARES": 5.0,
                "BUY_MAX_SPEND": 3.0,
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
                    round(float(ask) - 0.01, 4), 10.0, float(ask), 10.0, None,
                ),
                "entry_book_ok": lambda *_a, **_k: (True, "ok"),
                "safe_api_call": lambda fn, *a, **k: fn(*a, **k),
                "client": SimpleNamespace(
                    create_order=create_order,
                    post_order=post,
                ),
                "OrderArgs": lambda **kwargs: kwargs,
                "PartialCreateOrderOptions": lambda **kwargs: kwargs,
                "OrderType": SimpleNamespace(FAK="FAK"),
                "signed_order_id": lambda *_a, **_k: "order-buy",
                "extract_order_id": lambda _result: "order-buy",
                "confirm_fill_size": lambda *_a, **_k: 3.0,
                "fill_cost_usdc": lambda *_a, **_k: 2.50,
                "classify_fill_against_band": classify_fill_against_band,
                "time": SimpleNamespace(time=lambda: 1.0, sleep=lambda _s: None),
            }
        )
        return ns, calls

    def _run(self, ttm, ask, armed_max=0.99):
        live = decide_5m_entry(ttm, ask)
        retry_pins = [float(armed_max), 0.90]

        def pre_submit(_bid, fresh_ask, _attempt):
            band = decide_5m_entry(ttm, fresh_ask)
            pinned = buy_retry_fak_limit(armed_max, band)
            if pinned is None:
                return False, "ttm_or_ask_out_of_band"
            retry_pins[0] = float(pinned)
            retry_pins[1] = float(band.retry_min_price)
            return True, band.name

        ns, calls = self._buy_ns(ask)
        result = ns["buy_market_with_retry"](
            "token", 2.50, armed_max,
            min_price=0.90,
            max_retries=1,
            pre_submit=pre_submit,
            retry_pins=retry_pins,
        )
        return result, calls, live

    def test_44s_93_posts_99(self):
        result, calls, live = self._run(44, 0.93)
        self.assertIsNotNone(live)
        self.assertEqual(live.name, "late_90")
        self.assertEqual(calls["post"], 1)
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.99)
        self.assertEqual(result[2], "filled")

    def test_46s_93_rejects_without_post(self):
        result, calls, live = self._run(46, 0.93)
        self.assertIsNone(live)
        self.assertEqual(calls["post"], 0)
        self.assertEqual(calls["orders"], [])
        self.assertEqual(result[2], "aborted")

    def test_44s_90_stays_fak_90_not_99(self):
        result, calls, live = self._run(44, 0.90)
        self.assertIsNotNone(live)
        self.assertEqual(live.name, "late")
        self.assertEqual(calls["post"], 1)
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.90)
        self.assertNotAlmostEqual(calls["orders"][0]["price"], 0.99)
        self.assertEqual(result[2], "filled")


class BotWiresCurrentRails(unittest.TestCase):
    def test_5m_uses_slug_clock_and_dump_helpers(self):
        src = BOT5M.read_text()
        self.assertIn("entry_seconds_left(", src)
        self.assertIn("evaluate_held_bag(", src)
        self.assertIn("recovery_cancel=", src)
        self.assertIn("hedge_skip_recovery", src)
        self.assertIn("HEDGE_RECOVERY_CANCEL", src)
        self.assertIn("pick_held_quote(", src)
        self.assertIn("stamp_slice_on_inventory(", src)
        self.assertIn("hedge_fail_is_terminal(", src)
        self.assertIn("dump=dump", src)
        self.assertIn("persist_done=", src)
        self.assertIn("market_tick=", src)
        self.assertIn("max_retries=12 if dump", src)
        self.assertIn('"hedge_threshold": 0.50', src)
        self.assertIn('"hedge_require_ask_max": 0.52', src)
        self.assertIn('"hedge_persist_s": 5.0', src)
        self.assertIn('"hedge_toxic_bid_max": 0.32', src)
        self.assertIn('"hedge_recovery_cancel": 0.53', src)
        self.assertIn('"hedge_sell_fade": True', src)
        self.assertIn('"hedge_require_oracle": True', src)
        self.assertIn('"hedge_dump_ignore_oracle": True', src)
        self.assertIn('"late_90_start_s": 45', src)
        self.assertIn("hold_while_oracle_agrees(", src)
        self.assertIn("hedge_dump_overrides_oracle(", src)
        self.assertIn("hedge_rest_required(", src)
        self.assertIn("hedge_oracle_blocks_sell(", src)
        self.assertIn("buy_retry_fak_limit(", src)
        self.assertIn("validate_late_90_start_s(", src)
        self.assertIn("retry_pins=", src)
        self.assertIn("hedge_skip_oracle_still_winning", src)
        self.assertIn("sell_fade=HEDGE_SELL_FADE", src)
        self.assertIn("pre_submit=_hedge_pre_submit", src)
        self.assertIn("TTM {seconds_left", src)
        self.assertNotIn("TTM {minutes_left", src)
        self.assertNotIn("last_dump", src)
        self.assertNotIn("oracle_dump_only", src)
        self.assertNotIn("late_cap_now_90", src)
        self.assertNotIn(
            "seconds_left = (end_ts_ms - now_ms) / 1000",
            src,
        )
        self.assertNotIn("late_ask_above_90", src)

    def test_invalid_late_90_start_s_rejected(self):
        with self.assertRaisesRegex(ValueError, "late_90_start_s must be >= 0"):
            validate_late_90_start_s(-1, 120)
        with self.assertRaisesRegex(ValueError, "late_90_start_s must be <= buy_start_s"):
            validate_late_90_start_s(200, 120)

    def test_dump_composition_helpers_match_review_cases(self):
        self.assertTrue(hedge_rest_required(persist_done=False, peek_dump=False))
        self.assertFalse(hedge_rest_required(persist_done=False, peek_dump=True))
        self.assertFalse(
            hedge_oracle_blocks_sell(
                dump=True, oracle_agrees=True, dump_ignore_oracle=True,
            )
        )
        intent = held_hedge_decision(
            0.32, 0.90, None, None, None, None,
            now_s=20.0, persist_armed_ts=None, persist_done=False,
            oracle_agrees=True, dump_ignore_oracle=True,
            dump_bid_max=0.32, qualify_bid=0.50, qualify_ask_max=0.52,
            recovery_cancel=0.53, persist_s=5.0, sell_fade=True,
        )
        self.assertEqual(intent.action, "dump")

    def test_hourly_uses_persist_50_52_and_dump_helpers(self):
        src = BOT_HR.read_text()
        self.assertIn("evaluate_held_bag(", src)
        self.assertIn("hedge_skip_recovery", src)
        self.assertIn("HEDGE_RECOVERY_CANCEL", src)
        self.assertIn("pick_held_quote(", src)
        self.assertIn("hedge_fail_is_terminal(", src)
        self.assertIn("dump=dump", src)
        self.assertIn("persist_done=", src)
        self.assertIn("market_tick=", src)
        self.assertIn("max_retries=12 if dump", src)
        self.assertIn('"hedge_threshold": 0.50', src)
        self.assertIn('"hedge_require_ask_max": 0.52', src)
        self.assertIn('"hedge_persist_s": 5.0', src)
        self.assertIn('"hedge_recovery_cancel": 0.53', src)
        self.assertIn('"hedge_sell_fade": True', src)
        self.assertIn('"hedge_require_oracle": True', src)
        self.assertIn("hold_while_oracle_agrees(", src)
        self.assertIn("hedge_skip_oracle_still_winning", src)
        self.assertIn('"b15_window_min": 20.0', src)
        self.assertIn('"a22_window_min": 0.0', src)
        self.assertNotIn(
            "seconds_left = (end_ts_ms - now_ms) / 1000",
            src,
        )


if __name__ == "__main__":
    unittest.main()
