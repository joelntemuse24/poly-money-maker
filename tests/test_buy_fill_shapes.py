"""Production-path response/shape tests for BUY fill + GC/hedge helpers.

Bots cannot be imported (no ``__main__`` guard). Critical pure helpers are
loaded by extracting function source from ``buybot.py`` so tests exercise the
real implementations, not a duplicated fork.
"""

from __future__ import annotations

import ast
import json
import math
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from types import SimpleNamespace

BOT = Path(__file__).resolve().parents[1] / "buybot.py"
BOT5M = Path(__file__).resolve().parents[1] / "buybot5m.py"
BOT_HR = Path(__file__).resolve().parents[1] / "buybothourly.py"


def _load_funcs(*names: str, bot: Path = BOT):
    src = bot.read_text()
    tree = ast.parse(src)
    wanted = set(names)
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            chunks.append(ast.get_source_segment(src, node))
            wanted.discard(node.name)
    if wanted:
        raise RuntimeError(f"missing functions in {bot.name}: {sorted(wanted)}")
    ns: dict = {
        "Decimal": Decimal,
        "ROUND_DOWN": ROUND_DOWN,
        "InvalidOperation": InvalidOperation,
        "json": json,
        "math": math,
        "os": os,
        "shutil": shutil,
        "POLYMARKET_GUI_SPREAD": 0.10,
        "datetime": datetime,
    }
    exec(compile("\n\n".join(chunks), str(bot), "exec"), ns, ns)
    return ns


def _load_strategy_sign_lists(src: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Positive (<=0 reject) and non-negative (<0 reject) key lists in load_strategy."""
    tree = ast.parse(src)
    func = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "load_strategy"
    )
    positive: tuple[str, ...] | None = None
    nonnegative: tuple[str, ...] | None = None
    for node in ast.walk(func):
        if not isinstance(node, ast.For):
            continue
        if not isinstance(node.target, ast.Name) or node.target.id != "key":
            continue
        if not isinstance(node.iter, ast.Tuple):
            continue
        keys = tuple(
            elt.value
            for elt in node.iter.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        )
        if not keys:
            continue
        msg_bits: list[str] = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Raise):
                continue
            if not isinstance(child.exc, ast.Call) or not child.exc.args:
                continue
            arg0 = child.exc.args[0]
            if isinstance(arg0, ast.JoinedStr):
                for part in arg0.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        msg_bits.append(part.value)
        msg = "".join(msg_bits)
        if "must be positive" in msg:
            positive = keys
        elif "must be non-negative" in msg:
            nonnegative = keys
    if positive is None or nonnegative is None:
        raise RuntimeError("could not extract load_strategy sign lists")
    return positive, nonnegative


HELPERS = (
    "finite_float",
    "_decode_clob_fixed6",
    "_decode_clob_response_amount",
    "_string_list",
    "_fill_fee_usdc",
    "_load_trade_details",
    "_confirmed_trade_financials",
    "buy_shares_from_result",
    "_result_as_dict",
    "fill_cost_usdc",
    "entry_book_ok",
    "hedge_book_ok",
    "toxic_dump_book_ok",
    "polymarket_display_price",
    "hedge_consensus_ok",
    "quoted_buy_shares",
    "buy_fill_walked",
    "classify_buy_fill",
    "implied_buy_average",
    "reconcile_hedge_sold",
    "stable_zero_balances",
    "gc_par_redeem",
    "gc_can_finalize",
)


class BuyFillProductionHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_funcs(*HELPERS)
        cls.ns["_trade_detail_cache"] = {}
        cls.ns["_market_fee_cache"] = {}
        cls.ns["safe_api_call"] = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        cls.ns["log_event"] = lambda *_a, **_k: None
        cls.ns["client"] = SimpleNamespace(
            get_trades=lambda *_a, **_k: [],
            get_clob_market_info=lambda *_a, **_k: {},
        )
        cls.ns["TradeParams"] = lambda **kwargs: kwargs

    def test_matched_without_size_matched_uses_taking_amount(self):
        buy_shares = self.ns["buy_shares_from_result"]
        fill_cost = self.ns["fill_cost_usdc"]
        result = {
            "makingAmount": "4900000",
            "takingAmount": "5000000",
            "status": "matched",
        }
        shares = buy_shares(result)
        self.assertAlmostEqual(shares, 5.0)
        self.assertAlmostEqual(fill_cost(result, shares, 0.98, 8.0), 4.90)

    def test_delayed_zero_making_estimates_cost(self):
        buy_shares = self.ns["buy_shares_from_result"]
        fill_cost = self.ns["fill_cost_usdc"]
        result = {"makingAmount": "0", "takingAmount": "0", "status": "delayed"}
        self.assertEqual(buy_shares(result), 0.0)
        result2 = {"makingAmount": "0", "takingAmount": "5000000"}
        shares = buy_shares(result2)
        self.assertAlmostEqual(shares, 5.0)
        cost = fill_cost(result2, shares, 0.98, 8.0)
        self.assertAlmostEqual(cost, min(8.0, 5.0 * 0.98))
        self.assertGreater(cost, 0.0)

    def test_fixed_point_taking_amount_from_raw_post(self):
        """Captured-style 1e6 fixed-point amounts must normalize to shares."""
        decode = self.ns["_decode_clob_fixed6"]
        buy_shares = self.ns["buy_shares_from_result"]
        fill_cost = self.ns["fill_cost_usdc"]
        self.assertAlmostEqual(decode("5000000"), 5.0)
        self.assertAlmostEqual(decode(4900000), 4.9)
        # Immediate POST with fixed-point taking/making (no size_matched).
        result = {
            "takingAmount": "5000000",
            "makingAmount": "4900000",
            "status": "matched",
        }
        shares = buy_shares(result)
        self.assertAlmostEqual(shares, 5.0)
        self.assertAlmostEqual(fill_cost(result, shares, 0.98, 8.0), 4.9)

    def test_get_order_size_matched_fixed_point(self):
        decode = self.ns["_decode_clob_fixed6"]
        # GET-order style size_matched in fixed point.
        self.assertAlmostEqual(decode("10000000"), 10.0)
        self.assertAlmostEqual(decode("11000"), 0.011)

    def test_get_order_details_normalizes_fixed_point(self):
        ns = _load_funcs(
            "finite_float",
            "_decode_clob_response_amount",
            "_string_list",
            "get_order_details",
        )
        ns["client"] = SimpleNamespace(
            get_order=lambda _oid: {
                "status": "matched",
                "size_matched": "10000000",
                "size": "12000000",
            }
        )
        ns["safe_api_call"] = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        details = ns["get_order_details"]("order-1", expected_size=12.0)
        self.assertAlmostEqual(details["size_matched"], 10.0)
        self.assertAlmostEqual(details["size"], 12.0)

    def test_tiny_sell_wire_fill_cannot_become_thousands_of_shares(self):
        ns = _load_funcs(
            "finite_float",
            "_decode_clob_response_amount",
            "_string_list",
            "_result_as_dict",
            "buy_shares_from_result",
            "confirm_fill_size",
        )
        ns["log_event"] = lambda *_a, **_k: None
        ns["_trade_settlement_state"] = lambda _ids: "confirmed"
        ns["_confirmed_trade_financials"] = lambda *_a, **_k: None
        filled = ns["confirm_fill_size"](
            {
                "status": "matched",
                "makingAmount": "11000",
                "tradeIDs": ["trade-1"],
            },
            "order-1",
            0.011,
            side="SELL",
        )
        self.assertAlmostEqual(filled, 0.011)

    def test_trade_settlement_requires_confirmed_finality(self):
        ns = _load_funcs(
            "_string_list",
            "_load_trade_details",
            "_trade_settlement_state",
        )
        trade = {}
        ns["_trade_detail_cache"] = {}
        ns["client"] = SimpleNamespace(get_trades=lambda *_a, **_k: [dict(trade)])
        ns["TradeParams"] = lambda **kwargs: kwargs
        ns["safe_api_call"] = lambda fn, *args, **kwargs: fn(*args, **kwargs)

        trade.update({
            "id": "trade-1",
            "status": "TRADE_STATUS_MATCHED",
            "transaction_hash": "0xabc",
        })
        self.assertEqual(ns["_trade_settlement_state"](["trade-1"]), "pending")

        ns["_trade_detail_cache"].clear()
        trade["status"] = "TRADE_STATUS_CONFIRMED"
        self.assertEqual(ns["_trade_settlement_state"](["trade-1"]), "confirmed")

        ns["_trade_detail_cache"].clear()
        trade["status"] = "CONFIRMED"
        self.assertEqual(ns["_trade_settlement_state"](["trade-1"]), "confirmed")

        ns["_trade_detail_cache"].clear()
        trade["transaction_hash"] = None
        self.assertEqual(ns["_trade_settlement_state"](["trade-1"]), "pending")

        ns["_trade_detail_cache"].clear()
        trade["transaction_hash"] = "0xabc"
        trade["status"] = "TRADE_STATUS_FAILED"
        self.assertEqual(ns["_trade_settlement_state"](["trade-1"]), "failed")

    def test_canceled_with_matched_size_is_not_empty(self):
        ns = _load_funcs(
            "finite_float",
            "_decode_clob_response_amount",
            "_string_list",
            "_result_as_dict",
            "buy_shares_from_result",
            "get_order_details",
            "confirm_fill_size",
        )
        ns["log_event"] = lambda *_a, **_k: None
        ns["_trade_settlement_state"] = lambda _ids: "pending"
        ns["_confirmed_trade_financials"] = lambda *_a, **_k: None
        ns["safe_api_call"] = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        ns["client"] = SimpleNamespace(
            get_order=lambda _oid: {
                "status": "canceled",
                "size_matched": "5.0",
                "size": "5.0",
                "associate_trades": ["trade-1"],
            },
        )
        clock = {"t": 0.0}

        def _now():
            clock["t"] += 0.3
            return clock["t"]

        ns["time"] = SimpleNamespace(time=_now, sleep=lambda _s: None)
        filled = ns["confirm_fill_size"](
            {"status": "delayed", "tradeIDs": ["trade-1"]},
            "order-1",
            5.0,
            wait_delayed_s=0.5,
            side="BUY",
        )
        # Unsettled matched size must time out as unconfirmed (0), not empty-terminal.
        self.assertEqual(filled, 0.0)

    def test_entry_rejects_wide_book(self):
        ok, why = self.ns["entry_book_ok"](0.01, 0.98, 0.05, 0.90)
        self.assertFalse(ok)
        self.assertEqual(why, "wide_spread")

    def test_hedge_rejects_penny_under_high_ask(self):
        ok, why = self.ns["hedge_book_ok"](0.01, 0.99, 0.65, 0.15, 0.70)
        self.assertFalse(ok)
        self.assertEqual(why, "ask_too_high")

    def test_hedge_accepts_tight_reversal(self):
        ok, why = self.ns["hedge_book_ok"](0.55, 0.62, 0.65, 0.15, 0.70)
        self.assertTrue(ok)
        self.assertEqual(why, "ok")

    def test_hedge_consensus_rejects_high_last_trade(self):
        # Tight 32/38 book looks reversed; last print 85¢ says it is not.
        ok, why = self.ns["hedge_consensus_ok"](
            0.32, 0.38, 0.85,
            0.62, 0.68, 0.65,
            held_gui_max=0.30, other_gui_min=0.70,
            min_edge=0.05, last_trade_max=0.40,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "last_trade_too_high")

    def test_hedge_consensus_rejects_incomplete_gui(self):
        ok, why = self.ns["hedge_consensus_ok"](
            0.25, 0.32, 0.25,
            None, None, None,
            held_gui_max=0.30, other_gui_min=0.70,
            min_edge=0.05, last_trade_max=0.40,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "incomplete_gui")

    def test_hedge_consensus_rejects_spoof_tight_book_high_print(self):
        ok, why = self.ns["hedge_consensus_ok"](
            0.32, 0.38, 0.85,
            0.70, 0.75, 0.72,
            held_gui_max=0.30, other_gui_min=0.70,
            min_edge=0.05, last_trade_max=0.40,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "last_trade_too_high")

    def test_hedge_consensus_accepts_real_reversal(self):
        # Last trade 25¢ + GUI 70/30 on the other/held legs.
        ok, why = self.ns["hedge_consensus_ok"](
            0.25, 0.32, 0.25,
            0.68, 0.72, 0.70,
            held_gui_max=0.30, other_gui_min=0.70,
            min_edge=0.05, last_trade_max=0.40,
        )
        self.assertTrue(ok)
        self.assertEqual(why, "ok")

    def test_quoted_buy_shares_is_budget_over_ask_not_top_size(self):
        quoted = self.ns["quoted_buy_shares"]
        # $2.50 at 80¢ → 3.10 shares ($2.48). 3.125 sh is $2.50 but the
        # SDK round_downs size to 2 dp → 3.12 * 80¢ = $2.496 (CLOB 400).
        self.assertAlmostEqual(quoted(2.50, 0.80), 3.10)
        self.assertAlmostEqual(quoted(2.50, 0.80, 5.0), 3.10)
        # Displayed top size is not an argument — a 1-sh book still sizes 3.10.
        # share_cap=1.0 is the tunable rail, not the book.
        self.assertAlmostEqual(quoted(2.50, 0.80, 1.0), 1.0)
        # $2.50 at 40¢ would be 6.25 sh — rail clips to 5.
        self.assertAlmostEqual(quoted(2.50, 0.40, 5.0), 5.0)
        self.assertEqual(quoted(2.50, 0.0, 5.0), 0.0)

    def test_quoted_buy_shares_maker_usdc_is_two_decimals(self):
        quoted = self.ns["quoted_buy_shares"]
        # Live 5m rejections: 3.0487 sh * 82¢ = $2.4999 (maker > 2 dp).
        for ask in (0.75, 0.79, 0.81, 0.82, 0.85, 0.86, 0.87, 0.89, 0.90):
            shares = quoted(2.50, ask, 5.0)
            self.assertGreaterEqual(shares, 0.01, ask)
            self.assertAlmostEqual(shares, round(shares, 2), places=9, msg=ask)
            maker = Decimal(str(shares)) * Decimal(str(ask))
            self.assertEqual(maker.quantize(Decimal("0.01")), maker, ask)
        self.assertLess(quoted(2.50, 0.82, 5.0) * 0.82, 2.5000001)
        self.assertAlmostEqual(quoted(2.50, 0.82), 3.0)

    def test_classify_buy_fill_walk_and_cheap_avg_are_toxic(self):
        classify = self.ns["classify_buy_fill"]
        below, toxic = classify(0.80, 3.12, 3.12, 0.75, 0.65)
        self.assertFalse(below)
        self.assertFalse(toxic)
        below, toxic = classify(0.09, 27.3, 3.12, 0.75, 0.65)
        self.assertTrue(below)
        self.assertTrue(toxic)
        below, toxic = classify(0.70, 3.12, 3.12, 0.75, 0.65)
        self.assertTrue(below)
        self.assertFalse(toxic)
        # Extra shares vs the posted quote are not a dump while avg stays
        # above the 65¢ floor (limit FAK at a better price).
        below, toxic = classify(0.91, 2.175822, 2.00, 0.90, 0.65)
        self.assertFalse(below)
        self.assertFalse(toxic)
        # Missing quote must not false-toxic a 3-sh fill.
        self.assertFalse(self.ns["buy_fill_walked"](3.12, None))
        self.assertFalse(self.ns["buy_fill_walked"](3.12, 0.0))
        self.assertFalse(self.ns["buy_fill_walked"](3.12, 3.125))
        # A USDC leftover decoded as 2.50 "shares" looks like a walk — callers
        # still persist quoted_buy_shares, not taker USDC, for buy_fill_walk
        # logs. That walk no longer arms toxic_fill.
        self.assertTrue(self.ns["buy_fill_walked"](3.12, 2.50))
        below, toxic = classify(0.80, 3.12, 2.50, 0.75, 0.65)
        self.assertFalse(below)
        self.assertFalse(toxic)

    def test_implied_average_is_usdc_over_shares_not_gate_ask(self):
        implied = self.ns["implied_buy_average"]
        self.assertAlmostEqual(implied(2.50, 27.3, 0.80), 2.50 / 27.3)
        self.assertAlmostEqual(implied(0.0, 27.3, 0.80), 0.80)

    def test_decode_does_not_crush_walked_human_shares_to_fixed_dust(self):
        decode = self.ns["_decode_clob_response_amount"]
        self.assertAlmostEqual(decode("27.3", expected=3.125), 27.3)
        self.assertAlmostEqual(decode("27300000", expected=3.125), 27.3)
        self.assertAlmostEqual(decode("3125000", expected=3.125), 3.125)
        self.assertAlmostEqual(decode("11000", expected=0.011), 0.011)

    def test_trade_financials_keep_buy_walk_vwap(self):
        ns = _load_funcs(
            "finite_float",
            "_decode_clob_response_amount",
            "_confirmed_trade_financials",
            "_fill_fee_usdc",
        )
        ns["_load_trade_details"] = lambda _ids: [{
            "status": "CONFIRMED",
            "transaction_hash": "0xabc",
            "size": "27.3",
            "price": 0.09,
        }]
        out = ns["_confirmed_trade_financials"](
            ["t1"], expected_size=3.125, fee_schedule=None,
        )
        self.assertAlmostEqual(out["shares"], 27.3)
        self.assertAlmostEqual(out["gross"], 27.3 * 0.09)

    def test_confirm_fill_size_accepts_buy_walk(self):
        ns = _load_funcs(
            "finite_float",
            "_decode_clob_response_amount",
            "_string_list",
            "_result_as_dict",
            "buy_shares_from_result",
            "confirm_fill_size",
        )
        ns["log_event"] = lambda *_a, **_k: None
        ns["_trade_settlement_state"] = lambda _ids: "confirmed"
        ns["_confirmed_trade_financials"] = lambda *_a, **_k: None
        filled = ns["confirm_fill_size"](
            {
                "status": "matched",
                "takingAmount": "27.3",
                "tradeIDs": ["trade-1"],
            },
            "order-1",
            3.125,
            side="BUY",
        )
        self.assertAlmostEqual(filled, 27.3)

    def test_inspect_uncertain_buy_walk_is_confirmed_not_mismatch(self):
        ns = _load_funcs(
            "finite_float",
            "_decode_clob_response_amount",
            "_string_list",
            "inspect_uncertain_order",
        )
        ns["get_order_details"] = lambda *_a, **_k: {
            "status": "matched",
            "size_matched": 27.3,
            "asset_id": "tok",
            "market": "cond",
            "side": "BUY",
            "price": 0.09,
            "trade_ids": ["t1"],
        }
        ns["_trade_settlement_state"] = lambda _ids: "confirmed"
        ns["_confirmed_trade_financials"] = lambda *_a, **_k: {
            "shares": 27.3, "gross": 2.457, "fee": 0.0,
        }
        ns["_fill_fee_usdc"] = lambda *_a, **_k: 0.0
        out = ns["inspect_uncertain_order"](
            "oid",
            side="BUY",
            requested=3.125,
            token_id="tok",
            condition_id="cond",
            limit_price=0.80,
            spend_cap=2.50,
        )
        self.assertEqual(out["state"], "confirmed")
        self.assertAlmostEqual(out["filled"], 27.3)
        self.assertAlmostEqual(out["value"], 2.457)

    def test_hedge_accepts_collapsed_20c_book(self):
        # 35/40 already passed conceptually: 20¢/30¢ is a real tight collapse.
        ok, why = self.ns["hedge_book_ok"](0.20, 0.30, 0.35, 0.15, 0.40)
        self.assertTrue(ok)
        self.assertEqual(why, "ok")
        ok, why = self.ns["hedge_book_ok"](0.01, 0.10, 0.35, 0.15, 0.40)
        self.assertTrue(ok)

    def test_toxic_dump_skips_recovered_book(self):
        dump_ok = self.ns["toxic_dump_book_ok"]
        # 01:13 case: winner bid 97¢ must not dump.
        self.assertFalse(dump_ok(0.97, 0.35))
        # 04:43 junk: 11¢ bid (even under a 99¢ ask) must still dump.
        self.assertTrue(dump_ok(0.11, 0.35))
        self.assertTrue(dump_ok(0.01, 0.35))
        self.assertTrue(dump_ok(0.35, 0.35))
        # Missing bid is fail-closed — no sell.
        self.assertFalse(dump_ok(None, 0.35))

    def test_hedge_sell_follows_live_bid_not_32c_floor(self):
        ns = _load_funcs("hedge_sell_price")
        ns["TICK_SIZE_FALLBACK"] = "0.001"
        sell = ns["hedge_sell_price"]
        self.assertAlmostEqual(sell(0.20, 0.001, 0, 0.001), 0.20)
        self.assertAlmostEqual(sell(0.20, 0.001, 2, 0.001), 0.198)
        # A leftover 32.5¢ config must not refuse the live bid.
        self.assertAlmostEqual(sell(0.20, 0.001, 0, 0.325), 0.20)
        self.assertAlmostEqual(sell(0.01, 0.001, 0, 0.325), 0.01)

    def test_5m_hedge_exec_tick_honors_clob_01(self):
        from buy.hedge_gate import hedge_market_tick

        ns = _load_funcs("hedge_exec_tick", "hedge_sell_price", bot=BOT5M)
        ns["EXPECTED_TICK_SIZE"] = "0.001"
        ns["TICK_SIZE_FALLBACK"] = "0.001"
        ns["hedge_market_tick"] = hedge_market_tick
        self.assertEqual(ns["hedge_exec_tick"]("0.01"), 0.01)
        self.assertEqual(ns["hedge_exec_tick"](0.01), 0.01)
        self.assertEqual(ns["hedge_exec_tick"]("0.001"), 0.001)
        # Live undercut is 0: 0.61 bid on a 0.01 book posts 0.61, not 0.001.
        self.assertAlmostEqual(ns["hedge_sell_price"](0.61, "0.01", 0, 0.01), 0.61)
        self.assertAlmostEqual(ns["hedge_sell_price"](0.612, "0.01", 0, 0.01), 0.61)
        # Undercut 2 on a 0.01 tick is the 21 Aug 0.51-into-0.53 hole — keep 0.
        self.assertAlmostEqual(ns["hedge_sell_price"](0.53, "0.01", 2, 0.01), 0.51)


class HedgeReconcileProduction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_funcs("reconcile_hedge_sold", "stable_zero_balances")

    def test_stale_high_api_does_not_erase_confirmed(self):
        # 10 confirmed sold; Data API still shows old bal=10 → api_sold=0.
        rec = self.ns["reconcile_hedge_sold"](10.0, 10.0, 5.5, 10.0, 0.55)
        self.assertAlmostEqual(rec["effective_sold"], 10.0)
        self.assertAlmostEqual(rec["proceeds"], 5.5)
        self.assertAlmostEqual(rec["rem"], 0.0)
        self.assertTrue(rec["lag"])
        self.assertFalse(rec["ghost_candidate"])

    def test_single_low_api_cannot_add_unconfirmed_tail(self):
        rec = self.ns["reconcile_hedge_sold"](10.0, 7.0, 3.85, 0.0, 0.55)
        self.assertAlmostEqual(rec["effective_sold"], 7.0)
        self.assertAlmostEqual(rec["proceeds"], 3.85)
        self.assertAlmostEqual(rec["rem"], 3.0)
        self.assertTrue(rec["ghost_candidate"])

    def test_single_zero_is_ghost_candidate_only(self):
        # Zero CLOB confirms + one zero balance must NOT invent a full exit.
        rec = self.ns["reconcile_hedge_sold"](10.0, 0.0, 0.0, 0.0, 0.55)
        self.assertAlmostEqual(rec["effective_sold"], 0.0)
        self.assertAlmostEqual(rec["proceeds"], 0.0)
        self.assertTrue(rec["ghost_candidate"])

    def test_zero_confirm_partial_api_drop_not_trusted(self):
        rec = self.ns["reconcile_hedge_sold"](10.0, 0.0, 0.0, 4.0, 0.55)
        self.assertAlmostEqual(rec["effective_sold"], 0.0)
        self.assertFalse(rec["ghost_candidate"])

    def test_stable_zero_requires_repeated_success(self):
        stable = self.ns["stable_zero_balances"]
        self.assertFalse(stable([0.0]))
        self.assertFalse(stable([0.0, None]))
        self.assertFalse(stable([0.0, 10.0]))
        self.assertTrue(stable([0.0, 0.0, 0.0]))

    def test_none_balance_keeps_confirms(self):
        rec = self.ns["reconcile_hedge_sold"](10.0, 8.0, 4.0, None, 0.5)
        self.assertAlmostEqual(rec["effective_sold"], 8.0)
        self.assertAlmostEqual(rec["proceeds"], 4.0)
        self.assertTrue(rec["balance_unverified"])


class GcParRedeemProduction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_funcs("gc_par_redeem", "gc_can_finalize")

    def test_skips_par_for_toxic_hedged_and_uncertain(self):
        gc = self.ns["gc_par_redeem"]
        self.assertEqual(gc({"toxic_fill": True, "bought_size": 50}, 0, 0), 0.0)
        self.assertEqual(gc({"hedge_closed": True, "bought_size": 0}, 10, 0), 0.0)
        self.assertEqual(gc({"bought_size": 20}, 5.0, 0), 0.0)
        self.assertEqual(gc({"hedge_attempted": True, "bought_size": 20}, 0, 0), 0.0)
        self.assertEqual(gc({"hedge_blocked_toxic": True, "bought_size": 20}, 0, 0), 0.0)
        self.assertEqual(gc({"buy_uncertain": True, "bought_size": 20}, 0, 0), 0.0)
        self.assertEqual(gc({"bought_size": 20}, 0, 0), 0.0)

    def test_explicit_redeem_preserved(self):
        gc = self.ns["gc_par_redeem"]
        self.assertEqual(gc({"hedge_attempted": True, "bought_size": 20}, 0, 15.0), 15.0)

    def test_gc_requires_terminal_execution_evidence(self):
        can_finalize = self.ns["gc_can_finalize"]
        self.assertFalse(can_finalize({"bought_size": 20}))
        self.assertFalse(can_finalize({"buy_uncertain": True, "bought_size": 20}))
        self.assertTrue(can_finalize({"hedge_closed": True, "bought_size": 0}))
        self.assertTrue(can_finalize({"pnl_redeem_value": 20.0}))


class StatePersistenceProduction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_funcs(
            "_reject_json_constant",
            "_parse_json_float",
            "atomic_save",
            "load_json",
        )

    def test_atomic_save_rejects_non_finite_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            with self.assertRaises(ValueError):
                self.ns["atomic_save"](path, {"unsafe": float("nan")})
            self.assertFalse(Path(path).exists())

    def test_load_json_recovers_from_overflowing_primary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text('{"unsafe": 1e10000}')
            Path(f"{path}.bak").write_text('{"safe": 1.25}')
            loaded = self.ns["load_json"](str(path), required=True)
            self.assertEqual(loaded, {"safe": 1.25})


class AmbiguousCrossCyclePolicy(unittest.TestCase):
    def test_quarantine_markers_in_all_bots(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertIn('buy_status == "ambiguous"', src)
            self.assertIn('meta["buy_uncertain"] = True', src)
            self.assertIn("buy_uncertain_token", src)
            self.assertIn("Quarantine is unconditional", src)
            self.assertIn('via="stable_held"', src)
            self.assertIn("ambiguous POST — no further retries", src)
            self.assertIn("buy_abort_no_baseline", src)
            self.assertIn("aborting further buys", src)
            self.assertIn("on_submit=_persist_buy_submit", src)
            self.assertIn('meta["buy_uncertain_baseline"]', src)
            self.assertIn("non-terminal 0-fill response — quarantined", src)
            self.assertIn('not positions_meta[c].get("buy_uncertain")', src)
            self.assertIn("held_size > uncertain_baseline + 0.01", src)
            self.assertIn("if meta.get(\"buy_uncertain\"):", src)
            self.assertIn("on_submit=_persist_hedge_submit", src)
            self.assertIn("on_fill=_persist_hedge_fill", src)
            self.assertIn("fetch_all_position_rows()", src)
            self.assertIn("hedge_ghost_unconfirmed", src)
            self.assertIn("inspect_uncertain_order(", src)
            self.assertIn("buy_uncertain_order_id", src)
            self.assertIn("hedge_uncertain_order_id", src)
            self.assertIn("buy_uncertain_trade_ids", src)
            self.assertIn("hedge_skip_ambiguous_legs", src)
            self.assertIn("def hedge_consensus_ok", src)
            self.assertIn("hedge_skip_no_consensus", src)
            self.assertIn('"hedge_require_gui": True', src)
            self.assertIn("STATE_MINED", src)
            self.assertIn("_clob_lock", src)

    def test_known_cost_assigned_before_buy_uncertain_spend_cap(self):
        needle = "min(float(BUY_BUDGET), float(BUY_MAX_SPEND)) - known_cost"
        assign = 'known_cost = float(meta.get("buy_uncertain_known_cost")'
        src15 = BOT.read_text()
        use = src15.find(needle)
        self.assertGreater(use, -1, BOT.name)
        defined = src15.rfind(assign, 0, use)
        self.assertGreater(defined, -1, BOT.name)
        for bot in (BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertIn("uncertain_buy_spend_cap(", src, bot.name)
            bot_assign = src.find(assign)
            bot_use = src.find("spend_cap=uncertain_buy_spend_cap(", bot_assign)
            self.assertGreater(bot_assign, -1, bot.name)
            self.assertGreater(bot_use, bot_assign, bot.name)

    def test_cycle_error_logs_error_field(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            idx = 0
            found = 0
            while True:
                idx = src.find('"cycle_error"', idx)
                if idx < 0:
                    break
                found += 1
                chunk = src[max(0, idx - 280): idx + 320]
                self.assertIn("except Exception as exc:", chunk, bot.name)
                self.assertIn('error=f"{type(exc).__name__}: {exc}"[:180]', chunk, bot.name)
                self.assertIn("traceback=traceback.format_exc()", chunk, bot.name)
                idx += len('"cycle_error"')
            self.assertGreaterEqual(found, 2, bot.name)

    def test_toxic_recovered_and_market_cycle_isolation(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertIn("hedge_skip_toxic_recovered", src, bot.name)
            self.assertIn("def toxic_dump_book_ok", src, bot.name)
            if bot in (BOT5M, BOT_HR):
                self.assertIn("evaluate_held_bag(", src, bot.name)
                self.assertIn("pick_held_quote(", src, bot.name)
            else:
                self.assertGreaterEqual(src.count("toxic_dump_book_ok("), 3, bot.name)
            self.assertIn("condition_id=cond", src, bot.name)
            self.assertNotIn(
                "remaining markets skipped this poll",
                src,
                bot.name,
            )

            tree = ast.parse(src)
            market_fors = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.For)
                and isinstance(node.target, ast.Name)
                and node.target.id == "m"
                and isinstance(node.iter, ast.Name)
                and node.iter.id in {"markets", "_loop_markets"}
                and node.end_lineno
                and (node.end_lineno - node.lineno) > 500
            ]
            self.assertEqual(len(market_fors), 1, bot.name)
            if bot in (BOT5M, BOT_HR):
                self.assertEqual(market_fors[0].iter.id, "_loop_markets")
            try_nodes = [
                stmt for stmt in market_fors[0].body if isinstance(stmt, ast.Try)
            ]
            self.assertTrue(try_nodes, bot.name)
            handler_src = ast.get_source_segment(src, try_nodes[0].handlers[0])
            self.assertIsNotNone(handler_src, bot.name)
            self.assertIn('"cycle_error"', handler_src, bot.name)
            self.assertIn("condition_id=cond", handler_src, bot.name)
            self.assertIn('error=f"{type(exc).__name__}: {exc}"[:180]', handler_src, bot.name)
            self.assertNotIn("remaining markets skipped this poll", handler_src, bot.name)

        five = BOT5M.read_text()
        self.assertIn("entry_seconds_left(", five)
        self.assertNotIn("seconds_left = (end_ts_ms - now_ms) / 1000", five)
        self.assertIn("applicable_entry_bands", five)
        self.assertIn("EARLY_BUY_START_S", five)
        self.assertIn("EARLY_95_START_S", five)
        self.assertIn("late_max=BUY_MAX_PRICE", five)
        self.assertNotIn("late_max=EARLY_BUY_MAX_PRICE", five)
        self.assertIn("LATE_BUY_BUDGET", five)
        self.assertIn("stamp_slice_bought", five)
        self.assertIn("accumulate_buy_inventory", five)
        self.assertIn("buy_skip_other_leg", five)
        self.assertIn("buy_skip_add_below_min", five)
        self.assertIn("hedge_skip_persist", five)
        self.assertIn('"hedge_threshold": 0.50', five)
        self.assertIn('"hedge_require_ask_max": 0.52', five)
        self.assertIn('"hedge_persist_s": 5.0', five)
        self.assertIn('"hedge_toxic_bid_max": 0.32', five)
        self.assertIn('"hedge_recovery_cancel": 0.53', five)
        self.assertIn('"hedge_sell_fade": True', five)
        self.assertIn('"hedge_require_oracle": True', five)
        self.assertIn('"hedge_dump_ignore_oracle": True', five)
        self.assertIn('"late_90_start_s": 45', five)
        self.assertIn("hold_while_oracle_agrees(", five)
        self.assertIn("hedge_dump_overrides_oracle(", five)
        self.assertIn("hedge_skip_oracle_still_winning", five)
        self.assertIn('"add_min_price": 0.90', five)
        self.assertIn('"hedge_undercut_ticks": 0', five)
        self.assertNotIn(
            "up_ask_ok = up_ask is not None and BUY_THRESHOLD <= up_ask <= BUY_MAX_PRICE",
            five,
        )
        src15 = BOT.read_text()
        self.assertNotIn("seconds_left = (end_ts_ms - now_ms) / 1000", src15)
        self.assertIn("minutes_left = (end_ts_ms - now_ms) / 60000", src15)
        self.assertIn(
            "up_ask_ok = up_ask is not None and BUY_THRESHOLD <= up_ask <= BUY_MAX_PRICE",
            src15,
        )
        hourly = BOT_HR.read_text()
        self.assertNotIn("seconds_left = (end_ts_ms - now_ms) / 1000", hourly)
        self.assertIn("minutes_left = (end_ts_ms - now_ms) / 60000", hourly)
        self.assertNotIn(
            "up_ask_ok = up_ask is not None and BUY_THRESHOLD <= up_ask <= BUY_MAX_PRICE",
            hourly,
        )
        self.assertIn("ask_in_any_band", hourly)
        self.assertIn("select_hourly_entry_band", hourly)
        self.assertIn("stamp_hourly_slice_bought", hourly)
        self.assertIn("applicable_hourly_entry_bands", hourly)
        self.assertIn('"hedge_threshold": 0.50', hourly)
        self.assertIn('"hedge_require_ask_max": 0.52', hourly)
        self.assertIn('"hedge_persist_s": 5.0', hourly)
        self.assertIn('"hedge_toxic_bid_max": 0.35', hourly)
        self.assertIn('"hedge_recovery_cancel": 0.53', hourly)
        self.assertIn('"hedge_sell_fade": True', hourly)
        self.assertIn('"hedge_require_oracle": True', hourly)
        self.assertIn("hold_while_oracle_agrees(", hourly)
        self.assertIn("hedge_skip_oracle_still_winning", hourly)
        self.assertIn('"hedge_undercut_ticks": 0', hourly)
        self.assertIn("evaluate_held_bag(", hourly)
        self.assertIn("hedge_skip_persist", hourly)
        self.assertIn("slice=band.name", hourly)
        self.assertIn("spent_so_far", hourly)
        self.assertIn("band.fak_limit", hourly)

    def test_hedge_liveness_and_reconcile_markers(self):
        src = BOT.read_text()
        self.assertIn("HEDGE_QUOTE_MAX_AGE_S", src)
        self.assertIn("ws_fresh = quote_age is not None and quote_age <= float(HEDGE_QUOTE_MAX_AGE_S)", src)
        self.assertIn("one read never adds/erases confirms", src)
        self.assertIn("reconcile_hedge_sold(", src)
        self.assertIn("stable_zero_balances(", src)
        self.assertIn("gc_par_redeem(", src)

    def test_hedge_fak_follows_live_bid_after_integrity(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertIn("hedge_floor = float(hedge_tick)", src, bot.name)
            self.assertNotIn(
                "else float(HEDGE_MIN_PRICE)",
                src,
                bot.name,
            )

    def test_all_siblings_persist_exact_order_intent_before_post(self):
        for bot in (BOT, BOT5M, BOT_HR):
            tree = ast.parse(bot.read_text())
            nested = {
                node.name: node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name in {"_persist_buy_submit", "_persist_hedge_submit"}
            }
            self.assertEqual(len(nested["_persist_buy_submit"].args.args), 5, bot.name)
            self.assertEqual(len(nested["_persist_hedge_submit"].args.args), 6, bot.name)

            src = bot.read_text()
            self.assertGreaterEqual(src.count("inspect_uncertain_order("), 3, bot.name)
            self.assertIn("expected_condition_id=cond", src, bot.name)
            self.assertIn('ENTRY_ENABLED = _strat["entry_enabled"]', src, bot.name)

    def test_buy_is_budget_limit_fak_not_top_capped(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertEqual(src.count("client.create_order"), 1, bot.name)
            self.assertEqual(src.count("client.create_market_order"), 1, bot.name)
            self.assertIn("quoted_buy_shares(", src, bot.name)
            self.assertIn("size=shares", src, bot.name)
            self.assertIn("buy_fill_walk", src, bot.name)
            self.assertIn("toxic_fill_armed_from_inventory", src, bot.name)
            self.assertIn('reason="no_bid"', src, bot.name)
            self.assertIn('quoted = meta.get("quoted_buy_shares")', src, bot.name)
            self.assertIn("BUY_MAX_SHARES", src, bot.name)
            if bot == BOT5M:
                self.assertIn('"buy_max_spend": 3.0', src, bot.name)
                self.assertIn('"buy_max_shares": 5.0', src, bot.name)
                self.assertIn("quoted_buy_shares_up_to_limit(", src, bot.name)
                self.assertIn("price=limit_price", src, bot.name)
                self.assertNotIn(
                    "quoted_buy_shares(remaining_budget, fresh_ask, BUY_MAX_SHARES)",
                    src,
                    bot.name,
                )
                order_chunk = src[src.index("OrderArgs(") : src.index("OrderArgs(") + 420]
                self.assertNotIn("user_usdc_balance", order_chunk, bot.name)
                self.assertIn('ROUNDING_CONFIG.get("0.001")', src, bot.name)
                self.assertIn("_tick_001.amount = 2", src, bot.name)
            elif bot == BOT_HR:
                self.assertIn('"buy_max_spend": 11.0', src, bot.name)
                self.assertIn('"buy_max_shares": 14.0', src, bot.name)
                self.assertIn("quoted_buy_shares_up_to_limit(", src, bot.name)
                self.assertIn("price=limit_price", src, bot.name)
                self.assertNotIn(
                    "quoted_buy_shares(remaining_budget, fresh_ask, BUY_MAX_SHARES)",
                    src,
                    bot.name,
                )
                order_chunk = src[src.index("OrderArgs(") : src.index("OrderArgs(") + 420]
                self.assertNotIn("user_usdc_balance", order_chunk, bot.name)
                self.assertIn('ROUNDING_CONFIG.get("0.01")', src, bot.name)
                self.assertIn("_tick_01.amount = 2", src, bot.name)
            else:
                self.assertIn('"buy_max_spend": 3.0', src, bot.name)
                self.assertIn('"buy_max_shares": 5.0', src, bot.name)
                self.assertIn(
                    "quoted_buy_shares(remaining_budget, fresh_ask, BUY_MAX_SHARES)",
                    src,
                    bot.name,
                )
                self.assertIn("price = fresh_ask", src, bot.name)
                self.assertNotIn("quoted_buy_shares_up_to_limit(", src, bot.name)
                self.assertIn("user_usdc_balance=remaining_budget", src, bot.name)
            self.assertIn("[THIN ASK]", src, bot.name)
            self.assertNotIn("min(budget / ask, ask_size)", src, bot.name)
            self.assertNotIn("[NO SIZE]", src, bot.name)
            self.assertNotIn(
                'quoted = meta.get("quoted_buy_shares") or meta.get(',
                src,
                bot.name,
            )

    def test_all_siblings_isolate_refresh_and_entry_io(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertIn("_io_executor = ThreadPoolExecutor", src, bot.name)
            self.assertIn("def _discover_markets_snapshot():", src, bot.name)
            self.assertIn("_entry_executor = ThreadPoolExecutor", src, bot.name)
            self.assertIn("fut_up = _entry_executor.submit(", src, bot.name)
            self.assertIn("if not _discovery_fresh:", src, bot.name)
            self.assertIn(
                "not bool(\n"
                "                positions_meta.get(market.condition_id, {}).get(\"buy_uncertain\")",
                src,
                bot.name,
            )

    def test_last_trade_fallback_has_a_freshness_bound(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertIn(
                "now - cached[1] <= _BOOK_SNAPSHOT_MAX_AGE_S",
                src,
                bot.name,
            )
            self.assertIn(
                "return get_book_snapshot_last_trade(token_id)",
                src,
                bot.name,
            )


class BuyExecutionAmbiguity(unittest.TestCase):
    @staticmethod
    def _namespace(response):
        ns = _load_funcs(
            "finite_float",
            "_result_as_dict",
            "quoted_buy_shares",
            "buy_fill_walked",
            "classify_buy_fill",
            "implied_buy_average",
            "buy_market_with_retry",
            "unmatched_fak_rejection",
            "definitive_order_rejection",
        )
        calls = {"post": 0, "submit": [], "orders": []}
        signed_order = SimpleNamespace(
            makerAmount="8000000",
            takerAmount="8163265",
            timestamp="1",
        )

        def post(*_args, **_kwargs):
            calls["post"] += 1
            return response

        def create_order(args, **_kwargs):
            calls["orders"].append(args)
            return signed_order

        ns.update(
            {
                "DRY_RUN": False,
                "BUY": "BUY",
                "BUY_MAX_SHARES": 5.0,
                "HEDGE_GHOST_SLEEP_S": 0.0,
                "MAX_ENTRY_SPREAD": 0.10,
                "MIN_WINNER_BID": 0.90,
                "TOXIC_FORCE_EXIT_BELOW": 0.65,
                "console": SimpleNamespace(print=lambda *_a, **_k: None),
                "log_event": lambda *_a, **_k: None,
                "check_token_balance": lambda *_args: 0.0,
                "check_clob_token_balance": lambda *_args, **_kwargs: 0.0,
                "_fill_fee_usdc": lambda *_args, **_kwargs: None,
                "get_quote_fast": lambda *_a, **_k: (0.97, None, 0.98, 100.0, None),
                "entry_book_ok": lambda *_a, **_k: (True, "ok"),
                "safe_api_call": lambda fn, *a, **k: fn(*a, **k),
                "client": SimpleNamespace(
                    create_order=create_order,
                    post_order=post,
                ),
                "OrderArgs": lambda **kwargs: kwargs,
                "PartialCreateOrderOptions": lambda **kwargs: kwargs,
                "OrderType": SimpleNamespace(FAK="FAK"),
                "signed_order_id": lambda *_a, **_k: "order-1",
                "extract_order_id": lambda _result: "order-1",
                "confirm_fill_size": lambda *_a, **_k: 0.0,
                "fill_cost_usdc": lambda *_a, **_k: 0.0,
                "time": SimpleNamespace(time=lambda: 1.0, sleep=lambda _s: None),
            }
        )
        return ns, calls

    def test_truthy_delayed_zero_fill_is_quarantined(self):
        ns, calls = self._namespace(
            {"status": "delayed", "orderID": "order-1", "makingAmount": "0", "takingAmount": "0"}
        )

        def on_submit(*args):
            calls["submit"].append(args)

        result = ns["buy_market_with_retry"](
            "token", 8.0, 0.99, min_price=0.96, max_retries=1,
            on_submit=on_submit,
        )
        self.assertEqual(result, (0.0, 0.0, "ambiguous"))
        self.assertEqual(calls["post"], 1)
        self.assertEqual(len(calls["submit"]), 1)
        self.assertEqual(calls["submit"][0][0], 0.0)  # persisted baseline
        self.assertEqual(len(calls["orders"]), 1)
        self.assertIn("size", calls["orders"][0])
        self.assertNotIn("amount", calls["orders"][0])

    def test_share_cap_clips_oversized_budget_at_ask(self):
        ns, calls = self._namespace({"status": "unmatched", "orderID": "order-1"})
        ns["BUY_MAX_SHARES"] = 5.0
        ns["get_quote_fast"] = lambda *_a, **_k: (0.79, 0.5, 0.80, 100.0, None)
        result = ns["buy_market_with_retry"](
            "token", 20.0, 0.90, min_price=0.75, max_retries=1,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(result, (0.0, 0.0, "empty"))
        self.assertEqual(len(calls["orders"]), 1)
        self.assertAlmostEqual(calls["orders"][0]["size"], 5.0)
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.80)

    def test_thin_displayed_ask_still_posts_budget_shares(self):
        ns, calls = self._namespace({"status": "unmatched", "orderID": "order-1"})
        ns["get_quote_fast"] = lambda *_a, **_k: (0.79, 0.5, 0.80, 0.01, None)
        result = ns["buy_market_with_retry"](
            "token", 2.50, 0.90, min_price=0.75, max_retries=1,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(result, (0.0, 0.0, "empty"))
        self.assertEqual(len(calls["orders"]), 1)
        self.assertAlmostEqual(calls["orders"][0]["size"], 3.10)
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.80)

    def test_explicit_unmatched_zero_fill_is_terminal_empty(self):
        ns, calls = self._namespace({"status": "unmatched", "orderID": "order-1"})
        result = ns["buy_market_with_retry"](
            "token", 8.0, 0.99, min_price=0.96, max_retries=1,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(result, (0.0, 0.0, "empty"))
        self.assertEqual(calls["post"], 1)

    def test_write_ahead_failure_prevents_post(self):
        ns, calls = self._namespace({"status": "matched"})

        def fail_submit(*_args):
            raise OSError("disk full")

        result = ns["buy_market_with_retry"](
            "token", 8.0, 0.99, min_price=0.96, max_retries=1,
            on_submit=fail_submit,
        )
        self.assertEqual(result, (0.0, 0.0, "persist_fail"))
        self.assertEqual(calls["post"], 0)

    def test_positive_delayed_fill_stays_quarantined(self):
        ns, calls = self._namespace(
            {"status": "delayed", "orderID": "order-1", "takingAmount": "5000000"}
        )
        ns["confirm_fill_size"] = lambda *_a, **_k: 5.0
        ns["fill_cost_usdc"] = lambda *_a, **_k: 4.9
        persisted = []
        result = ns["buy_market_with_retry"](
            "token", 8.0, 0.99, min_price=0.96, max_retries=2,
            on_submit=lambda *args: calls["submit"].append(args),
            on_fill=lambda bought, spent: persisted.append((bought, spent)),
        )
        self.assertEqual(result, (5.0, 4.9, "ambiguous"))
        self.assertEqual(calls["post"], 1)
        self.assertEqual(persisted, [(5.0, 4.9)])

    def test_unmatched_fak_rejection_only_matches_empty_fak_400(self):
        ns = _load_funcs("unmatched_fak_rejection")

        class Fake(Exception):
            def __init__(self, status, msg):
                super().__init__(msg)
                self.status_code = status

        self.assertTrue(
            ns["unmatched_fak_rejection"](
                Fake(
                    400,
                    "no orders found to match with FAK order. "
                    "FAK orders are partially filled or killed if no match is found.",
                )
            )
        )
        self.assertFalse(
            ns["unmatched_fak_rejection"](
                Fake(400, "invalid amounts, max accuracy of 2 decimals")
            )
        )
        self.assertFalse(ns["unmatched_fak_rejection"](Fake(401, "no orders found to match")))
        self.assertFalse(ns["unmatched_fak_rejection"](Fake(400, "not enough balance")))

    def test_unmatched_fak_400_retries_up_to_max(self):
        ns, calls = self._namespace({"status": "unmatched"})

        class Unmatched(Exception):
            status_code = 400

            def __str__(self):
                return (
                    "PolyApiException[status_code=400, error_message="
                    "{'error': 'no orders found to match with FAK order'}]"
                )

        def post(*_a, **_k):
            calls["post"] += 1
            raise Unmatched()

        ns["client"] = SimpleNamespace(
            create_order=ns["client"].create_order,
            post_order=post,
        )
        ns["get_quote_fast"] = lambda *_a, **_k: (0.79, 0.5, 0.80, 10.0, None)
        result = ns["buy_market_with_retry"](
            "token", 2.50, 0.90, min_price=0.75, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(result, (0.0, 0.0, "empty"))
        self.assertEqual(calls["post"], 3)
        self.assertEqual(len(calls["submit"]), 3)

    def test_invalid_amount_400_does_not_retry(self):
        ns, calls = self._namespace({"status": "unmatched"})

        class InvalidAmt(Exception):
            status_code = 400

            def __str__(self):
                return "invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals"

        def post(*_a, **_k):
            calls["post"] += 1
            raise InvalidAmt()

        ns["client"] = SimpleNamespace(
            create_order=ns["client"].create_order,
            post_order=post,
        )
        ns["get_quote_fast"] = lambda *_a, **_k: (0.79, 0.5, 0.80, 10.0, None)
        result = ns["buy_market_with_retry"](
            "token", 2.50, 0.90, min_price=0.75, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(result, (0.0, 0.0, "empty"))
        self.assertEqual(calls["post"], 1)

    def test_unmatched_400_stops_if_balance_appeared(self):
        ns, calls = self._namespace({"status": "unmatched"})

        class Unmatched(Exception):
            status_code = 400

            def __str__(self):
                return "no orders found to match with FAK order"

        def post(*_a, **_k):
            calls["post"] += 1
            raise Unmatched()

        ns["client"] = SimpleNamespace(
            create_order=ns["client"].create_order,
            post_order=post,
        )
        ns["get_quote_fast"] = lambda *_a, **_k: (0.79, 0.5, 0.80, 10.0, None)
        bal_calls = {"n": 0}

        def bal(*_a, **_k):
            bal_calls["n"] += 1
            return 0.0 if bal_calls["n"] == 1 else 3.05

        ns["check_clob_token_balance"] = bal
        result = ns["buy_market_with_retry"](
            "token", 2.50, 0.90, min_price=0.75, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
            on_fill=lambda *_a: calls.setdefault("fills", []).append(_a),
        )
        self.assertEqual(result[2], "filled")
        self.assertEqual(calls["post"], 1)
        self.assertGreater(result[0], 0)

    def test_unmatched_400_no_balance_does_not_retry(self):
        ns, calls = self._namespace({"status": "unmatched"})

        class Unmatched(Exception):
            status_code = 400

            def __str__(self):
                return "no orders found to match with FAK order"

        def post(*_a, **_k):
            calls["post"] += 1
            raise Unmatched()

        ns["client"] = SimpleNamespace(
            create_order=ns["client"].create_order,
            post_order=post,
        )
        ns["get_quote_fast"] = lambda *_a, **_k: (0.79, 0.5, 0.80, 10.0, None)
        bal_calls = {"n": 0}

        def bal(*_a, **_k):
            bal_calls["n"] += 1
            return 0.0 if bal_calls["n"] == 1 else None

        ns["check_clob_token_balance"] = bal
        result = ns["buy_market_with_retry"](
            "token", 2.50, 0.90, min_price=0.75, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(result, (0.0, 0.0, "ambiguous"))
        self.assertEqual(calls["post"], 1)

    def test_all_siblings_retry_unmatched_fak_400(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertIn("def unmatched_fak_rejection(exc):", src, bot.name)
            self.assertIn("unmatched_retry=can_retry", src, bot.name)
            self.assertGreaterEqual(src.count("unmatched_retry=can_retry"), 2, bot.name)
            self.assertIn('via="unmatched_400_no_balance"', src, bot.name)
            self.assertIn("empty_fak_cooldown_s", src, bot.name)
            self.assertIn("last_buy_empty", src, bot.name)
            self.assertIn("[FAK EMPTY]", src, bot.name)
            self.assertIn("hedge no match · re-quote", src, bot.name)

    def test_5m_sell_default_tick(self):
        src = BOT5M.read_text()
        # Default on sell_market_with_retry must be 0.001 for 5m markets.
        start = src.index("def sell_market_with_retry(")
        chunk = src[start : start + 250]
        self.assertIn('tick_size="0.001"', chunk)

    def test_5m_hedge_posts_at_bid_not_2c_undercut(self):
        src = BOT5M.read_text()
        self.assertIn("def hedge_exec_tick(", src)
        self.assertIn("hedge_tick = hedge_exec_tick(", src)
        self.assertIn("undercut_ticks=0,", src)
        self.assertIn('"hedge_undercut_ticks": 0', src)
        self.assertIn("hedge_tick_after_build_error", src)
        self.assertIn("hedge_tick_retry", src)


class FiveMinuteHedgeGuiTests(unittest.TestCase):
    """5m hedge GUI follows ask-max / complement, not buy 70/30."""

    @classmethod
    def setUpClass(cls):
        cls.ns = _load_funcs(
            "finite_float",
            "polymarket_display_price",
            "hedge_gui_limits",
            "hedge_consensus_ok",
            bot=BOT5M,
        )

    def test_gui_limits_are_ask_max_and_complement(self):
        self.assertEqual(self.ns["hedge_gui_limits"](0.55), (0.55, 0.45))
        self.assertEqual(self.ns["hedge_gui_limits"](0.40), (0.40, 0.60))

    def test_accepts_53c_stop_while_held_still_slightly_ahead(self):
        # Tight 52/54 (mid 53) vs 46/48 (mid 47): the 53¢ stop, not a 30¢ loser.
        held_max, other_min = self.ns["hedge_gui_limits"](0.55)
        ok, why = self.ns["hedge_consensus_ok"](
            0.52, 0.54, 0.52,
            0.46, 0.48, 0.47,
            held_gui_max=held_max, other_gui_min=other_min,
            min_edge=0.05, last_trade_max=0.55,
        )
        self.assertTrue(ok)
        self.assertEqual(why, "ok")

    def test_still_rejects_85c_last_trade_on_tight_53c_book(self):
        held_max, other_min = self.ns["hedge_gui_limits"](0.55)
        ok, why = self.ns["hedge_consensus_ok"](
            0.52, 0.54, 0.85,
            0.46, 0.48, 0.47,
            held_gui_max=held_max, other_gui_min=other_min,
            min_edge=0.05, last_trade_max=0.55,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "last_trade_too_high")

    def test_still_rejects_other_leg_that_has_not_repriced(self):
        held_max, other_min = self.ns["hedge_gui_limits"](0.55)
        ok, why = self.ns["hedge_consensus_ok"](
            0.52, 0.54, 0.52,
            0.20, 0.25, 0.22,
            held_gui_max=held_max, other_gui_min=other_min,
            min_edge=0.05, last_trade_max=0.55,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "other_gui_too_low")

    def test_5m_call_site_uses_ask_max_complement_not_buy_70_30(self):
        src = BOT5M.read_text()
        self.assertIn("def hedge_gui_limits(", src)
        self.assertIn("hedge_gui_limits(HEDGE_REQUIRE_ASK_MAX)", src)
        self.assertNotIn("held_gui_max=MAX_LOSER_BID", src)
        self.assertNotIn("other_gui_min=MIN_WINNER_BID", src)
        start = src.find("def hedge_consensus_ok(")
        end = src.find("\ndef quoted_buy_shares(", start)
        self.assertNotIn('return False, "gui_not_reversed"', src[start:end])
        self.assertIn('return False, "gui_not_reversed"', BOT.read_text())
        hourly_src = BOT_HR.read_text()
        hr_start = hourly_src.find("def hedge_consensus_ok(")
        hr_end = hourly_src.find("\ndef quoted_buy_shares(", hr_start)
        self.assertNotIn('return False, "gui_not_reversed"', hourly_src[hr_start:hr_end])
        self.assertIn("hedge_gui_limits(HEDGE_REQUIRE_ASK_MAX)", hourly_src)
        self.assertNotIn("other_gui_min=MIN_WINNER_BID", hourly_src)


class FiveMinuteBandLimitFakTests(unittest.TestCase):
    """5m FAK limit is the open band max (90¢ late, 99¢ early); size stays budget/ask."""

    def test_sizes_at_ask_maker_valid_at_band_max(self):
        ns = _load_funcs(
            "finite_float",
            "quoted_buy_shares",
            "quoted_buy_shares_up_to_limit",
            bot=BOT5M,
        )
        fn = ns["quoted_buy_shares_up_to_limit"]
        cases = (
            (0.75, 0.90),   # last 120s 75–90
            (0.83, 0.90),   # last 120s, live miss at 83¢
            (0.88, 0.90),   # live 20 Aug 19:58 (was 2.00 sh / $1.80)
            (0.90, 0.99),   # first 3 min ≥90 (was 2.00 sh / $1.98)
            (0.91, 0.99),
            (0.96, 0.99),   # ≥95 overlay
        )
        for ask, limit in cases:
            shares = fn(2.50, ask, limit, 5.0, spend_cap=3.0)
            self.assertGreaterEqual(shares, 3.0, ask)
            self.assertLessEqual(shares * limit, 3.0 + 1e-9, ask)
            maker = Decimal(str(shares)) * Decimal(str(limit))
            self.assertEqual(maker.quantize(Decimal("0.01")), maker, ask)
            self.assertGreaterEqual(float(maker), 2.50, ask)

    def test_worst_case_spend_clips_to_buy_max_spend(self):
        ns = _load_funcs(
            "finite_float",
            "quoted_buy_shares",
            "quoted_buy_shares_up_to_limit",
            bot=BOT5M,
        )
        shares = ns["quoted_buy_shares_up_to_limit"](
            2.50, 0.75, 0.99, 5.0, spend_cap=3.0,
        )
        unclipped = ns["quoted_buy_shares"](2.50, 0.75, 5.0)
        self.assertLess(shares, unclipped)
        self.assertLessEqual(shares * 0.99, 3.0 + 1e-9)

    @staticmethod
    def _namespace():
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
        from buy.entry_skip import classify_fill_against_band
        ns["classify_fill_against_band"] = classify_fill_against_band
        calls = {"post": 0, "submit": [], "orders": []}
        signed_order = SimpleNamespace(
            makerAmount="8000000",
            takerAmount="8163265",
            timestamp="1",
        )

        def post(*_args, **_kwargs):
            calls["post"] += 1
            return {"status": "unmatched", "orderID": "order-1"}

        def create_order(args, **_kwargs):
            calls["orders"].append(args)
            return signed_order

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
                "check_token_balance": lambda *_args: 0.0,
                "check_clob_token_balance": lambda *_args, **_kwargs: 0.0,
                "_fill_fee_usdc": lambda *_args, **_kwargs: None,
                "entry_book_ok": lambda *_a, **_k: (True, "ok"),
                "safe_api_call": lambda fn, *a, **k: fn(*a, **k),
                "client": SimpleNamespace(
                    create_order=create_order,
                    post_order=post,
                ),
                "OrderArgs": lambda **kwargs: kwargs,
                "PartialCreateOrderOptions": lambda **kwargs: kwargs,
                "OrderType": SimpleNamespace(FAK="FAK"),
                "signed_order_id": lambda *_a, **_k: "order-1",
                "extract_order_id": lambda _result: "order-1",
                "confirm_fill_size": lambda *_a, **_k: 0.0,
                "fill_cost_usdc": lambda *_a, **_k: 0.0,
                "time": SimpleNamespace(time=lambda: 1.0, sleep=lambda _s: None),
            }
        )
        return ns, calls

    def test_late_window_posts_90_limit_sized_at_83_ask(self):
        ns, calls = self._namespace()
        ns["get_quote_fast"] = lambda *_a, **_k: (0.82, 0.5, 0.83, 10.0, None)
        result = ns["buy_market_with_retry"](
            "token", 2.50, 0.90, min_price=0.75, max_retries=1,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(result, (0.0, 0.0, "empty"))
        self.assertEqual(len(calls["orders"]), 1)
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.90)
        self.assertGreaterEqual(calls["orders"][0]["size"], 3.0)
        self.assertLessEqual(calls["orders"][0]["size"] * 0.90, 3.0 + 1e-9)

    def test_early_90_and_95_windows_also_limit_at_99(self):
        ns, calls = self._namespace()
        ns["get_quote_fast"] = lambda *_a, **_k: (0.90, 0.5, 0.91, 10.0, None)
        ns["buy_market_with_retry"](
            "token", 2.50, 0.99, min_price=0.90, max_retries=1,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.99)
        self.assertAlmostEqual(calls["orders"][0]["size"], 3.0)
        calls["orders"].clear()
        ns["get_quote_fast"] = lambda *_a, **_k: (0.95, 0.5, 0.96, 10.0, None)
        ns["buy_market_with_retry"](
            "token", 2.50, 0.99, min_price=0.95, max_retries=1,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertAlmostEqual(calls["orders"][0]["price"], 0.99)
        self.assertAlmostEqual(calls["orders"][0]["size"], 3.0)

    def test_early_99_limit_omits_fake_usdc_balance(self):
        ns, calls = self._namespace()
        ns["get_quote_fast"] = lambda *_a, **_k: (0.90, 0.5, 0.91, 10.0, None)
        ns["buy_market_with_retry"](
            "token", 2.50, 0.99, min_price=0.90, max_retries=1,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(len(calls["orders"]), 1)
        order = calls["orders"][0]
        self.assertNotIn("user_usdc_balance", order)
        self.assertAlmostEqual(order["price"], 0.99)
        self.assertAlmostEqual(order["size"], 3.0)

    def test_early_90_limit_fill_at_91_is_not_toxic(self):
        """Live 20 Aug 19:42 was 2.00 sh / $1.98; new sizer posts 3.00 / $2.97."""
        ns = _load_funcs(
            "finite_float",
            "quoted_buy_shares",
            "quoted_buy_shares_up_to_limit",
            "buy_fill_walked",
            "classify_buy_fill",
            bot=BOT5M,
        )
        from buy.entry_skip import classify_fill_against_band
        ns["classify_fill_against_band"] = classify_fill_against_band
        posted = ns["quoted_buy_shares_up_to_limit"](
            2.50, 0.90, 0.99, 5.0, spend_cap=3.0,
        )
        self.assertAlmostEqual(posted, 3.0)
        filled = 2.97 / 0.91
        below, toxic = ns["classify_buy_fill"](0.91, filled, posted, 0.90, 0.65)
        self.assertFalse(below)
        self.assertFalse(toxic)
        late_posted = ns["quoted_buy_shares_up_to_limit"](
            2.50, 0.88, 0.90, 5.0, spend_cap=3.0,
        )
        self.assertAlmostEqual(late_posted, 3.0)
        below, toxic = ns["classify_buy_fill"](
            0.90, late_posted, late_posted, 0.75, 0.65,
        )
        self.assertFalse(below)
        self.assertFalse(toxic)


class HourlyBandLimitFakTests(unittest.TestCase):
    """Hourly FAK: A/C limit 99¢ ($5 or remaining to $10), B limit 90¢."""

    def test_sizes_maker_valid_at_99_and_90(self):
        ns = _load_funcs(
            "finite_float",
            "quoted_buy_shares",
            "quoted_buy_shares_up_to_limit",
            bot=BOT_HR,
        )
        fn = ns["quoted_buy_shares_up_to_limit"]
        five = fn(5.0, 0.94, 0.99, 14.0, spend_cap=5.0)
        self.assertGreaterEqual(five, 3.0)
        self.assertLessEqual(five * 0.99, 5.0 + 1e-9)
        maker_a = Decimal(str(five)) * Decimal("0.99")
        self.assertEqual(maker_a.quantize(Decimal("0.01")), maker_a)
        self.assertAlmostEqual(five, 5.0)
        self.assertEqual(float(maker_a), 4.95)

        ten = fn(10.0, 0.88, 0.90, 14.0, spend_cap=10.0)
        self.assertGreaterEqual(ten, 3.0)
        self.assertLessEqual(ten * 0.90, 10.0 + 1e-9)
        maker_b = Decimal(str(ten)) * Decimal("0.90")
        self.assertEqual(maker_b.quantize(Decimal("0.01")), maker_b)
        self.assertAlmostEqual(ten, 11.10)
        self.assertEqual(float(maker_b), 9.99)

        ten_flat_c = fn(10.0, 0.96, 0.99, 14.0, spend_cap=10.0)
        maker_c = Decimal(str(ten_flat_c)) * Decimal("0.99")
        self.assertEqual(maker_c.quantize(Decimal("0.01")), maker_c)
        self.assertLessEqual(float(maker_c), 10.0 + 1e-9)
        self.assertGreaterEqual(float(maker_c), 9.0)

    @staticmethod
    def _namespace():
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
        calls = {"post": 0, "submit": [], "orders": []}
        signed_order = SimpleNamespace(
            makerAmount="4950000",
            takerAmount="5000000",
            timestamp="1",
        )

        def post(*_args, **_kwargs):
            calls["post"] += 1
            return {"status": "unmatched", "orderID": "order-1"}

        def create_order(args, **_kwargs):
            calls["orders"].append(args)
            return signed_order

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
                "check_token_balance": lambda *_args: 0.0,
                "check_clob_token_balance": lambda *_args, **_kwargs: 0.0,
                "_fill_fee_usdc": lambda *_args, **_kwargs: None,
                "entry_book_ok": lambda *_a, **_k: (True, "ok"),
                "safe_api_call": lambda fn, *a, **k: fn(*a, **k),
                "client": SimpleNamespace(
                    create_order=create_order,
                    post_order=post,
                ),
                "OrderArgs": lambda **kwargs: kwargs,
                "PartialCreateOrderOptions": lambda **kwargs: kwargs,
                "OrderType": SimpleNamespace(FAK="FAK"),
                "signed_order_id": lambda *_a, **_k: "order-1",
                "extract_order_id": lambda _result: "order-1",
                "confirm_fill_size": lambda *_a, **_k: 0.0,
                "fill_cost_usdc": lambda *_a, **_k: 0.0,
                "time": SimpleNamespace(time=lambda: 1.0, sleep=lambda _s: None),
            }
        )
        return ns, calls

    def test_slice_a_posts_99_limit_no_usdc_balance(self):
        ns, calls = self._namespace()
        ns["get_quote_fast"] = lambda *_a, **_k: (0.93, 0.5, 0.94, 10.0, None)
        result = ns["buy_market_with_retry"](
            "token", 5.0, 0.99, min_price=0.93, max_retries=1,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(result, (0.0, 0.0, "empty"))
        self.assertEqual(len(calls["orders"]), 1)
        order = calls["orders"][0]
        self.assertNotIn("user_usdc_balance", order)
        self.assertAlmostEqual(order["price"], 0.99)
        self.assertAlmostEqual(order["size"], 5.0)
        self.assertLessEqual(order["size"] * 0.99, 5.0 + 1e-9)

    def test_slice_b_posts_90_limit_ten_dollars(self):
        ns, calls = self._namespace()
        ns["get_quote_fast"] = lambda *_a, **_k: (0.87, 0.5, 0.88, 20.0, None)
        ns["buy_market_with_retry"](
            "token", 10.0, 0.90, min_price=0.75, max_retries=1,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual(len(calls["orders"]), 1)
        order = calls["orders"][0]
        self.assertNotIn("user_usdc_balance", order)
        self.assertAlmostEqual(order["price"], 0.90)
        self.assertAlmostEqual(order["size"], 11.10)
        self.assertLessEqual(order["size"] * 0.90, 10.0 + 1e-9)


class FiveMinuteClobMakerRoundingTests(unittest.TestCase):
    """Live 21 Aug 2026: 3.00 @ 99¢ + fake $2.97 wallet → maker $2.9601 → 400."""

    def tearDown(self):
        from py_clob_client_v2.clob_types import RoundConfig
        from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG

        ROUNDING_CONFIG["0.001"] = RoundConfig(price=3, size=2, amount=5)

    def _amounts(self, size, price, amount_dp):
        from py_clob_client_v2.clob_types import RoundConfig
        from py_clob_client_v2.order_builder.builder import OrderBuilder
        from py_clob_client_v2.order_builder.constants import BUY

        cfg = RoundConfig(price=3, size=2, amount=amount_dp)
        builder = OrderBuilder.__new__(OrderBuilder)
        _side, maker, taker = builder.get_order_amounts(BUY, size, price, cfg)
        return maker / 1e6, taker / 1e6

    def test_omitting_balance_keeps_three_shares_at_297_cents(self):
        usdc, shares = self._amounts(3.0, 0.99, 4)
        self.assertEqual(shares, 3.0)
        self.assertEqual(usdc, 2.97)
        self.assertEqual(Decimal(str(usdc)).as_tuple().exponent, -2)

    def test_fake_balance_equal_to_notional_makes_4dp_maker(self):
        from py_clob_client_v2.fees import adjust_buy_amount_for_fees

        price = 0.99
        spend = 3.0 * price
        balance = min(3.0, max(2.50, spend))
        adjusted = adjust_buy_amount_for_fees(spend, price, balance, 0.0, 0.0, 0.0)
        usdc, shares = self._amounts(adjusted / price, price, 4)
        self.assertEqual(shares, 2.99)
        self.assertEqual(usdc, 2.9601)
        self.assertGreater(Decimal(str(usdc)).as_tuple().exponent * -1, 2)

    def test_5m_amount_2_patch_rounds_dirty_maker_to_cents(self):
        usdc, shares = self._amounts(2.9999999999999996, 0.99, 2)
        self.assertEqual(shares, 2.99)
        self.assertEqual(usdc, 2.96)
        self.assertEqual(Decimal(str(usdc)).as_tuple().exponent, -2)


class SellExecutionAmbiguity(unittest.TestCase):
    @staticmethod
    def _namespace(response=None, post_error=None, confirmed=0.0):
        ns = _load_funcs("_result_as_dict", "sell_market_with_retry")
        calls = {"post": 0, "submit": [], "fill": []}
        signed_order = SimpleNamespace(
            makerAmount="10000000",
            takerAmount="5000000",
            timestamp="1",
        )

        def post(*_args, **_kwargs):
            calls["post"] += 1
            if post_error:
                raise post_error
            return response

        ns.update(
            {
                "DRY_RUN": False,
                "SELL": "SELL",
                "console": SimpleNamespace(print=lambda *_a, **_k: None),
                "log_event": lambda *_a, **_k: None,
                "hedge_exec_tick": lambda t: str(t or "0.001"),
                "hedge_tick_after_build_error": lambda *_a, **_k: None,
                "hedge_should_keep_retrying": lambda *_a, **_k: False,
                "hedge_sell_price": lambda _bid, _tick, _under, _floor: 0.50,
                "get_quote_fast": lambda *_a, **_k: (0.55, 10.0, 0.60, 10.0, 0.575),
                "hedge_book_ok": lambda *_a, **_k: (True, "ok"),
                "safe_api_call": lambda fn, *a, **k: fn(*a, **k),
                "client": SimpleNamespace(
                    create_market_order=lambda *_a, **_k: signed_order,
                    post_order=post,
                ),
                "MarketOrderArgs": lambda **kwargs: kwargs,
                "PartialCreateOrderOptions": lambda **kwargs: kwargs,
                "OrderType": SimpleNamespace(FAK="FAK"),
                "signed_order_id": lambda *_a, **_k: "order-sell",
                "unmatched_fak_rejection": lambda _exc: False,
                "definitive_order_rejection": lambda _exc: False,
                "check_clob_token_balance": lambda *_a, **_k: 10.0,
                "extract_order_id": lambda _result: "order-sell",
                "confirm_fill_size": lambda *_a, **_k: confirmed,
                "fill_proceeds": lambda *_a, **_k: confirmed * 0.50,
                "time": SimpleNamespace(sleep=lambda _s: None),
            }
        )
        return ns, calls

    def test_sell_exception_stops_without_retry(self):
        ns, calls = self._namespace(post_error=TimeoutError("accepted maybe"))
        sold, result, proceeds = ns["sell_market_with_retry"](
            "token", 10.0, 0.55, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual((sold, proceeds), (0.0, 0.0))
        self.assertEqual(result["bot_status"], "ambiguous")
        self.assertEqual(calls["post"], 1)
        self.assertEqual(len(calls["submit"]), 1)

    def test_positive_delayed_sell_is_persisted_then_quarantined(self):
        ns, calls = self._namespace(
            response={"status": "delayed", "orderID": "order-sell"},
            confirmed=4.0,
        )
        sold, result, proceeds = ns["sell_market_with_retry"](
            "token", 10.0, 0.55, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
            on_fill=lambda *args: calls["fill"].append(args),
        )
        self.assertEqual((sold, proceeds), (4.0, 2.0))
        self.assertEqual(result["bot_status"], "ambiguous")
        self.assertEqual(calls["post"], 1)
        self.assertEqual(calls["fill"], [(4.0, 2.0)])

    def test_sell_write_ahead_failure_prevents_post(self):
        ns, calls = self._namespace(response={"status": "matched"})

        def fail_submit(*_args):
            raise OSError("disk full")

        sold, result, proceeds = ns["sell_market_with_retry"](
            "token", 10.0, 0.55, on_submit=fail_submit,
        )
        self.assertEqual((sold, proceeds), (0.0, 0.0))
        self.assertEqual(result["bot_status"], "persist_fail")
        self.assertEqual(calls["post"], 0)

    def test_sell_unmatched_fak_400_retries_up_to_max(self):
        class Unmatched(Exception):
            status_code = 400

            def __str__(self):
                return "no orders found to match with FAK order"

        ns, calls = self._namespace(post_error=Unmatched())
        ns["unmatched_fak_rejection"] = lambda exc: "no orders found to match" in str(exc).lower()
        ns["check_clob_token_balance"] = lambda *_a, **_k: 3.2
        sold, result, proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.53, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual((sold, proceeds), (0.0, 0.0))
        self.assertEqual(result["bot_status"], "empty")
        self.assertEqual(calls["post"], 3)
        self.assertEqual(len(calls["submit"]), 3)

    def test_sell_invalid_amount_400_does_not_retry(self):
        class InvalidAmt(Exception):
            status_code = 400

            def __str__(self):
                return "invalid amounts, max accuracy of 2 decimals"

        ns, calls = self._namespace(post_error=InvalidAmt())
        ns["unmatched_fak_rejection"] = lambda exc: "no orders found to match" in str(exc).lower()
        ns["definitive_order_rejection"] = lambda exc: getattr(exc, "status_code", None) == 400
        sold, result, proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.53, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual((sold, proceeds), (0.0, 0.0))
        self.assertEqual(result["bot_status"], "empty")
        self.assertEqual(calls["post"], 1)

    def test_sell_unmatched_400_ghost_when_balance_drops(self):
        class Unmatched(Exception):
            status_code = 400

            def __str__(self):
                return "no orders found to match with FAK order"

        ns, calls = self._namespace(post_error=Unmatched())
        ns["unmatched_fak_rejection"] = lambda exc: "no orders found to match" in str(exc).lower()
        bals = [3.2, 0.0]

        def bal(*_a, **_k):
            return bals.pop(0) if bals else 0.0

        ns["check_clob_token_balance"] = bal
        sold, result, proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.53, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
            on_fill=lambda *args: calls["fill"].append(args),
        )
        self.assertEqual(result["bot_status"], "filled")
        self.assertAlmostEqual(sold, 3.2)
        self.assertEqual(calls["post"], 1)
        self.assertEqual(len(calls["fill"]), 1)

    def test_sell_unmatched_400_no_balance_does_not_retry(self):
        class Unmatched(Exception):
            status_code = 400

            def __str__(self):
                return "no orders found to match with FAK order"

        ns, calls = self._namespace(post_error=Unmatched())
        ns["unmatched_fak_rejection"] = lambda exc: "no orders found to match" in str(exc).lower()
        ns["check_clob_token_balance"] = lambda *_a, **_k: None
        sold, result, proceeds = ns["sell_market_with_retry"](
            "token", 3.2, 0.53, max_retries=3,
            on_submit=lambda *args: calls["submit"].append(args),
        )
        self.assertEqual((sold, proceeds), (0.0, 0.0))
        self.assertEqual(result["bot_status"], "ambiguous")
        self.assertEqual(calls["post"], 1)


class BalanceAndGcSemantics(unittest.TestCase):
    def test_absent_token_is_zero_on_success(self):
        def balance_from_positions(rows, token_id):
            if not isinstance(rows, list):
                return None
            for p in rows:
                if p.get("asset") == token_id:
                    return float(p.get("size", 0) or 0)
            return 0.0

        self.assertEqual(balance_from_positions([], "tok"), 0.0)
        self.assertEqual(balance_from_positions([{"asset": "other", "size": 1}], "tok"), 0.0)

    def test_runtime_safety_defaults_in_all_bots(self):
        for bot in (BOT, BOT5M, BOT_HR):
            src = bot.read_text()
            self.assertIn('"dry_run": True', src)
            self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", src)
            self.assertIn("os.fsync(f.fileno())", src)
            self.assertIn("os.fsync(dir_fd)", src)
            self.assertIn('os.getenv("POLY_BUILDER_API_KEY")', src)
            self.assertIn('os.getenv("RELAYER_API_KEY")', src)
            self.assertIn('os.getenv("RELAYER_API_KEY_ADDRESS")', src)
            self.assertIn("BuilderConfig(", src)
            self.assertIn('config.generate_builder_headers(', src)
            self.assertIn('"POST", "/submit", str(body)', src)
            self.assertIn('"RELAYER_API_KEY"', src)
            self.assertIn("RELAYER_API_KEY_ADDRESS", src)
            self.assertNotIn("019df62f-45bc-796e-975c-3f434472b163", src)
            self.assertIn("unknown strategy keys", src)
            self.assertIn("return _strat_cache", src)
            self.assertIn("parse_float=_parse_json_float", src)
            self.assertIn('sell_status == "persist_fail"', src)

    def test_examples_are_disarmed(self):
        root = BOT.parent
        for name in (
            "strategy_buy.example.json",
            "strategy_buy5m.example.json",
            "strategy_buyhourly.example.json",
        ):
            data = json.loads((root / name).read_text())
            self.assertIs(data["dry_run"], True)
            self.assertIs(data["entry_enabled"], False)
        five = json.loads((root / "strategy_buy5m.example.json").read_text())
        self.assertEqual(five["buy_start_s"], 45)
        self.assertEqual(five["early_buy_start_s"], 45)
        self.assertEqual(five["early_buy_max_price"], 0.99)
        self.assertEqual(five["early_95_start_s"], 0)
        self.assertEqual(five["early_95_min_s"], 0)
        self.assertEqual(five["early_95_min_price"], 0.95)
        self.assertEqual(five["late_90_start_s"], 45)
        self.assertEqual(five["min_underlying_edge_usd"], 25.0)
        self.assertEqual(five["hedge_threshold"], 0.50)
        self.assertEqual(five["hedge_require_ask_max"], 0.52)
        self.assertEqual(five["hedge_persist_s"], 5.0)
        self.assertEqual(five["hedge_toxic_bid_max"], 0.32)
        self.assertEqual(five["hedge_recovery_cancel"], 0.53)
        self.assertEqual(five["hedge_sell_fade"], True)
        self.assertEqual(five["hedge_require_oracle"], True)
        self.assertEqual(five["hedge_dump_ignore_oracle"], True)
        self.assertEqual(five["add_min_price"], 0.90)
        self.assertEqual(five["hedge_undercut_ticks"], 0)
        self.assertEqual(five["buy_threshold"], 0.75)
        self.assertEqual(five["buy_max_price"], 0.90)
        self.assertEqual(five["buy_budget"], 2.5)
        self.assertEqual(five["late_buy_budget"], 2.5)
        self.assertEqual(five["poll_buy_window_s"], 0.01)
        self.assertEqual(five["poll_held_s"], 0.01)
        self.assertEqual(five["ui_every_n_cycles"], 50)
        hourly = json.loads((root / "strategy_buyhourly.example.json").read_text())
        fifteen = json.loads((root / "strategy_buy.example.json").read_text())
        self.assertNotIn("early_buy_start_s", hourly)
        self.assertNotIn("early_buy_start_s", fifteen)
        self.assertNotIn("early_95_start_s", hourly)
        self.assertNotIn("early_95_start_s", fifteen)
        self.assertNotIn("late_buy_budget", hourly)
        self.assertNotIn("late_buy_budget", fifteen)
        self.assertEqual(hourly["a22_window_min"], 0.0)
        self.assertEqual(hourly["b15_window_min"], 20.0)
        self.assertEqual(hourly["c5_window_min"], 0.0)
        self.assertEqual(hourly["buy_window_min"], 20.0)
        self.assertEqual(hourly["a22_min_price"], 0.93)
        self.assertEqual(hourly["c5_min_price"], 0.95)
        self.assertEqual(hourly["high_buy_max_price"], 0.99)
        self.assertEqual(hourly["a22_buy_budget"], 5.0)
        self.assertEqual(hourly["b15_buy_budget"], 10.0)
        self.assertEqual(hourly["c5_buy_budget"], 10.0)
        self.assertEqual(hourly["market_spend_cap"], 10.0)
        self.assertEqual(hourly["buy_max_spend"], 11.0)
        self.assertEqual(hourly["buy_max_shares"], 14.0)
        self.assertEqual(hourly["buy_budget"], 10.0)
        self.assertEqual(hourly["hedge_threshold"], 0.50)
        self.assertEqual(hourly["hedge_require_ask_max"], 0.52)
        self.assertEqual(hourly["hedge_persist_s"], 5.0)
        self.assertEqual(hourly["hedge_toxic_bid_max"], 0.35)
        self.assertEqual(hourly["hedge_recovery_cancel"], 0.53)
        self.assertEqual(hourly["hedge_sell_fade"], True)
        self.assertEqual(hourly["hedge_require_oracle"], True)
        self.assertEqual(hourly["hedge_oracle_min_edge_usd"], 0.0)
        self.assertEqual(hourly["hedge_undercut_ticks"], 0)
        self.assertEqual(hourly["poll_buy_window_s"], 0.01)
        self.assertEqual(hourly["poll_held_s"], 0.01)
        self.assertEqual(hourly["min_underlying_edge_usd"], 10.0)
        self.assertEqual(hourly["tick_size"], "0.01")
        self.assertEqual(fifteen["hedge_threshold"], 0.35)

    def test_example_json_passes_5m_load_strategy_sign_rails(self):
        src = BOT5M.read_text()
        positive, nonnegative = _load_strategy_sign_lists(src)
        self.assertIn("buy_start_s", positive)
        self.assertIn("early_buy_start_s", positive)
        self.assertNotIn("early_95_start_s", positive)
        self.assertIn("early_95_start_s", nonnegative)
        self.assertIn("early_95_min_s", nonnegative)
        self.assertIn("late_90_start_s", nonnegative)
        data = json.loads((BOT5M.parent / "strategy_buy5m.example.json").read_text())
        for key in positive:
            self.assertGreater(float(data[key]), 0, key)
        for key in nonnegative:
            self.assertGreaterEqual(float(data[key]), 0, key)
        self.assertEqual(float(data["early_95_start_s"]), 0)
        self.assertGreaterEqual(
            float(data["early_95_start_s"]), float(data["early_95_min_s"]),
        )


class FiveMFastPollHelpers(unittest.TestCase):
    """Leftover 5m wallet bags must not be treated as live hedges."""

    @classmethod
    def setUpClass(cls):
        from buy.market import MintMarket

        cls.ns = _load_funcs(
            "finite_float",
            "meta_end_ts",
            "position_is_live_hedge",
            "uncertain_still_recoverable",
            "should_stub_tracked_market",
            "market_needs_fast_path",
            "bag_size",
            "count_wallet_bags",
            "count_live_hedges",
            "inventory_is_hot",
            "drop_wallet_dust",
            "pick_look_quote",
            "positions_refresh_interval_s",
            "positions_snapshot_is_fresh",
            "add_tracked_market_stubs",
            bot=BOT5M,
        )
        cls.ns["MintMarket"] = MintMarket
        cls.ns["SERIES_SLUG"] = "btc-updown-5m"

    def _bag(self, size=2.0, redeemable=False, token="up-tok"):
        return {
            "up": {
                "asset": token,
                "size": size,
                "redeemable": redeemable,
                "avgPrice": 0.9,
            },
            "dn": {
                "asset": "dn-tok",
                "size": 0.0,
                "redeemable": False,
                "avgPrice": 0.0,
            },
        }

    def test_redeemable_is_not_live_hedge(self):
        now = 1_700_000_000.0
        pos = self._bag(redeemable=True)
        meta = {"bought_token": "up-tok", "end_ts": now + 60}
        self.assertFalse(self.ns["position_is_live_hedge"](pos, meta, now))
        self.assertFalse(self.ns["should_stub_tracked_market"](pos, meta, now))

    def test_expired_dust_is_not_live_hedge(self):
        now = 1_700_000_000.0
        pos = self._bag()
        meta = {"bought_token": "up-tok", "end_ts": now - 120}
        self.assertFalse(self.ns["position_is_live_hedge"](pos, meta, now))
        self.assertFalse(self.ns["should_stub_tracked_market"](pos, meta, now))

    def test_no_meta_wallet_dust_is_not_live_hedge(self):
        now = 1_700_000_000.0
        pos = self._bag()
        self.assertFalse(self.ns["position_is_live_hedge"](pos, {}, now))
        self.assertFalse(self.ns["should_stub_tracked_market"](pos, {}, now))

    def test_this_bot_bag_in_window_is_live_hedge(self):
        now = 1_700_000_000.0
        pos = self._bag()
        meta = {"bought_token": "up-tok", "end_ts": now + 90}
        self.assertTrue(self.ns["position_is_live_hedge"](pos, meta, now))
        self.assertTrue(self.ns["should_stub_tracked_market"](pos, meta, now))

    def test_just_expired_stays_live_for_grace(self):
        now = 1_700_000_000.0
        pos = self._bag()
        meta = {"bought_token": "up-tok", "end_ts": now - 10}
        self.assertTrue(self.ns["position_is_live_hedge"](pos, meta, now))

    def test_bought_token_without_clock_is_not_live_hedge(self):
        now = 1_700_000_000.0
        pos = self._bag()
        meta = {
            "bought_token": "up-tok",
            "up_token": "up-tok",
            "dn_token": "dn-tok",
        }
        self.assertFalse(self.ns["position_is_live_hedge"](pos, meta, now))
        self.assertFalse(self.ns["should_stub_tracked_market"](pos, meta, now))
        self.assertEqual(
            self.ns["add_tracked_market_stubs"]([], {"old": pos}, {"old": meta}, now),
            [],
        )

    def test_stale_uncertain_is_not_stubbed(self):
        now = 1_700_000_000.0
        pos = self._bag(size=0.0)
        meta = {
            "buy_uncertain": True,
            "buy_uncertain_token": "up-tok",
            "end_ts": now - 86400,
        }
        self.assertFalse(self.ns["uncertain_still_recoverable"](meta, now))
        self.assertFalse(self.ns["should_stub_tracked_market"](pos, meta, now))
        self.assertEqual(
            self.ns["add_tracked_market_stubs"]([], {}, {"old": meta}, now),
            [],
        )

    def test_recent_uncertain_is_stubbed_without_quoting_as_live(self):
        now = 1_700_000_000.0
        pos = self._bag(size=0.0)
        meta = {
            "buy_uncertain": True,
            "buy_uncertain_token": "up-tok",
            "end_ts": now - 5,
        }
        self.assertFalse(self.ns["position_is_live_hedge"](pos, meta, now))
        self.assertTrue(self.ns["should_stub_tracked_market"](pos, meta, now))

    def test_stubs_skip_hundreds_of_clockless_bought_tokens(self):
        now = 1_700_000_000.0
        held = {f"c{i:03d}": self._bag() for i in range(704)}
        meta = {
            f"c{i:03d}": {"bought_token": "up-tok", "up_token": "up-tok", "dn_token": "dn-tok"}
            for i in range(704)
        }
        out = self.ns["add_tracked_market_stubs"]([], held, meta, now)
        self.assertEqual(out, [])
        self.assertEqual(self.ns["count_live_hedges"](held, meta, now), 0)

    def test_stubs_skip_hundreds_of_dust_bags(self):
        now = 1_700_000_000.0
        held = {f"c{i:03d}": self._bag() for i in range(704)}
        out = self.ns["add_tracked_market_stubs"]([], held, {}, now)
        self.assertEqual(out, [])
        self.assertEqual(self.ns["count_wallet_bags"](held), 704)
        self.assertEqual(self.ns["count_live_hedges"](held, {}, now), 0)

    def test_abandoned_ghosts_are_not_wait(self):
        now = 1_700_000_000.0
        held = {
            "ghost": self._bag(redeemable=True),
            "live": self._bag(token="live-up"),
        }
        meta = {
            "ghost": {"redeem_abandoned": True},
            "live": {"bought_token": "live-up", "end_ts": now + 45},
        }
        self.assertEqual(self.ns["count_wallet_bags"](held), 2)
        self.assertEqual(self.ns["count_wallet_bags"](held, meta), 1)
        self.assertEqual(self.ns["count_live_hedges"](held, meta, now), 1)

    def test_stubs_keep_live_hedge_and_not_dust(self):
        now = 1_700_000_000.0
        held = {
            "dust": self._bag(),
            "live": self._bag(token="live-up"),
        }
        meta = {
            "live": {
                "bought_token": "live-up",
                "end_ts": now + 45,
                "up_token": "live-up",
                "dn_token": "live-dn",
                "question": "BTC Up or Down 5m",
            },
        }
        out = self.ns["add_tracked_market_stubs"]([], held, meta, now)
        self.assertEqual([m.condition_id for m in out], ["live"])
        self.assertGreater(out[0].end_ts, now)
        self.assertEqual(self.ns["count_live_hedges"](held, meta, now), 1)
        self.assertEqual(self.ns["count_wallet_bags"](held), 2)

    def test_drop_wallet_dust_keeps_only_live_and_real_redeem(self):
        now = 1_700_000_000.0
        held = {f"c{i:03d}": self._bag() for i in range(666)}
        held["live"] = self._bag(token="live-up")
        held["cash"] = self._bag(redeemable=True)
        held["ghost"] = self._bag(redeemable=True)
        meta = {
            "live": {"bought_token": "live-up", "end_ts": now + 90},
            "ghost": {"redeem_abandoned": True},
        }
        kept = self.ns["drop_wallet_dust"](held, meta, now)
        self.assertEqual(set(kept), {"live", "cash"})
        self.assertEqual(self.ns["count_wallet_bags"](kept, meta), 2)
        self.assertEqual(self.ns["count_live_hedges"](kept, meta, now), 1)
        self.assertEqual(
            max(0, self.ns["count_wallet_bags"](kept, meta)
                - self.ns["count_live_hedges"](kept, meta, now)),
            1,
        )
        self.assertFalse(self.ns["inventory_is_hot"](held["c000"], {}, now))
        self.assertTrue(self.ns["inventory_is_hot"](held["live"], meta["live"], now))
        self.assertTrue(self.ns["inventory_is_hot"](held["cash"], {}, now))
        self.assertFalse(self.ns["inventory_is_hot"](held["ghost"], meta["ghost"], now))
        dust_only = self.ns["drop_wallet_dust"](
            {f"c{i:03d}": held[f"c{i:03d}"] for i in range(666)}, {}, now,
        )
        self.assertEqual(dust_only, {})
        self.assertEqual(self.ns["count_wallet_bags"](dust_only), 0)

    def test_fast_path_skips_far_gamma_slate(self):
        now = 1_700_000_000.0
        far = SimpleNamespace(end_ts=now + 3600, condition_id="far")
        near = SimpleNamespace(end_ts=now + 80, condition_id="near")
        self.assertFalse(
            self.ns["market_needs_fast_path"](far, {}, {}, now, 300.0)
        )
        self.assertTrue(
            self.ns["market_needs_fast_path"](near, {}, {}, now, 300.0)
        )

    def test_5m_source_poll_defaults_are_one_centisecond(self):
        src = BOT5M.read_text()
        self.assertIn('"poll_buy_window_s": 0.01', src)
        self.assertIn('"poll_held_s": 0.01', src)
        self.assertIn("for m in _loop_markets:", src)
        self.assertIn("drop_wallet_dust", src)
        self.assertIn("look_book_quote", src)
        self.assertIn("_HEARTBEAT_MIN_S", src)
        self.assertIn("buy_skip_rest_confirm", src)
        self.assertIn("stale_positions", src)
        self.assertIn("positions_snapshot_is_fresh", src)
        look_at = src.find("0.01s look: WS")
        rest_at = src.find("WS said buy. REST-confirm once")
        self.assertGreater(look_at, 0)
        self.assertGreater(rest_at, look_at)
        self.assertIn("_REDEEM_ENQUEUE_MIN_S", src)

    def test_pick_look_quote_prefers_ws_and_skips_empty(self):
        pick = self.ns["pick_look_quote"]
        ws = (0.80, 10.0, 0.81, 5.0, 0.805)
        cached = (0.50, 1.0, 0.90, 1.0, 0.70)
        self.assertEqual(pick(ws, cached), ws)
        self.assertEqual(pick(None, cached), cached)
        self.assertEqual(pick((None, 0.0, None, 0.0, None), cached), cached)
        self.assertEqual(
            pick(None, None),
            (None, 0.0, None, 0.0, None),
        )

    def test_positions_freshness_allows_slow_wallet_refresh(self):
        interval = self.ns["positions_refresh_interval_s"](1.0, False)
        self.assertEqual(interval, 15.0)
        self.assertTrue(
            self.ns["positions_snapshot_is_fresh"](100.0, 110.0, interval)
        )
        self.assertFalse(
            self.ns["positions_snapshot_is_fresh"](100.0, 160.0, interval)
        )
        fast = self.ns["positions_refresh_interval_s"](1.0, True)
        self.assertEqual(fast, 1.0)
        self.assertFalse(
            self.ns["positions_snapshot_is_fresh"](100.0, 106.0, fast)
        )
        self.assertFalse(self.ns["positions_snapshot_is_fresh"](0.0, 1.0, 15.0))


class HourlyFastPollHelpers(FiveMFastPollHelpers):
    """Hourly uses the same dust/live-hedge helpers; leftover bags are not POS."""

    @classmethod
    def setUpClass(cls):
        from buy.market import MintMarket

        cls.ns = _load_funcs(
            "finite_float",
            "meta_end_ts",
            "position_is_live_hedge",
            "uncertain_still_recoverable",
            "should_stub_tracked_market",
            "market_needs_fast_path",
            "bag_size",
            "count_wallet_bags",
            "count_live_hedges",
            "inventory_is_hot",
            "drop_wallet_dust",
            "pick_look_quote",
            "positions_refresh_interval_s",
            "positions_snapshot_is_fresh",
            "add_tracked_market_stubs",
            bot=BOT_HR,
        )
        cls.ns["MintMarket"] = MintMarket
        cls.ns["SERIES_SLUG"] = "btc-up-or-down-hourly"

    def test_5m_source_poll_defaults_are_one_centisecond(self):
        src = BOT_HR.read_text()
        self.assertIn('"poll_buy_window_s": 0.01', src)
        self.assertIn('"poll_held_s": 0.01', src)
        self.assertIn("for m in _loop_markets:", src)
        self.assertIn("drop_wallet_dust", src)
        self.assertIn("look_book_quote", src)
        self.assertIn("_HEARTBEAT_MIN_S", src)
        self.assertIn("buy_skip_rest_confirm", src)
        self.assertIn("stale_positions", src)
        self.assertIn("positions_snapshot_is_fresh", src)
        look_at = src.find("0.01s look: WS")
        rest_at = src.find("WS said buy. REST-confirm once")
        self.assertGreater(look_at, 0)
        self.assertGreater(rest_at, look_at)


if __name__ == "__main__":
    unittest.main()
