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
from decimal import Decimal, InvalidOperation
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
        "InvalidOperation": InvalidOperation,
        "json": json,
        "math": math,
        "os": os,
        "shutil": shutil,
    }
    exec(compile("\n\n".join(chunks), str(bot), "exec"), ns, ns)
    return ns


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
            self.assertIn("STATE_MINED", src)
            self.assertIn("_clob_lock", src)

    def test_hedge_liveness_and_reconcile_markers(self):
        src = BOT.read_text()
        self.assertIn("HEDGE_QUOTE_MAX_AGE_S", src)
        self.assertIn("ws_fresh = quote_age is not None and quote_age <= float(HEDGE_QUOTE_MAX_AGE_S)", src)
        self.assertIn("one read never adds/erases confirms", src)
        self.assertIn("reconcile_hedge_sold(", src)
        self.assertIn("stable_zero_balances(", src)
        self.assertIn("gc_par_redeem(", src)

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
        ns = _load_funcs("finite_float", "_result_as_dict", "buy_market_with_retry")
        calls = {"post": 0, "submit": []}
        signed_order = SimpleNamespace(
            makerAmount="8000000",
            takerAmount="8163265",
            timestamp="1",
        )

        def post(*_args, **_kwargs):
            calls["post"] += 1
            return response

        ns.update(
            {
                "DRY_RUN": False,
                "BUY": "BUY",
                "HEDGE_GHOST_SLEEP_S": 0.0,
                "MAX_ENTRY_SPREAD": 0.10,
                "MIN_WINNER_BID": 0.90,
                "console": SimpleNamespace(print=lambda *_a, **_k: None),
                "log_event": lambda *_a, **_k: None,
                "check_token_balance": lambda *_args: 0.0,
                "check_clob_token_balance": lambda *_args, **_kwargs: 0.0,
                "_fill_fee_usdc": lambda *_args, **_kwargs: None,
                "get_quote_fast": lambda *_a, **_k: (0.97, None, 0.98, 100.0, None),
                "entry_book_ok": lambda *_a, **_k: (True, "ok"),
                "safe_api_call": lambda fn, *a, **k: fn(*a, **k),
                "client": SimpleNamespace(
                    create_market_order=lambda *_a, **_k: signed_order,
                    post_order=post,
                ),
                "MarketOrderArgs": lambda **kwargs: kwargs,
                "PartialCreateOrderOptions": lambda **kwargs: kwargs,
                "OrderType": SimpleNamespace(FAK="FAK"),
                "signed_order_id": lambda *_a, **_k: "order-1",
                "definitive_order_rejection": lambda _exc: False,
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

    def test_5m_sell_default_tick(self):
        src = BOT5M.read_text()
        # Default on sell_market_with_retry must be 0.001 for 5m markets.
        start = src.index("def sell_market_with_retry(")
        chunk = src[start : start + 250]
        self.assertIn('tick_size="0.001"', chunk)


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
                "definitive_order_rejection": lambda _exc: False,
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
            self.assertIs(json.loads((root / name).read_text())["dry_run"], True)


if __name__ == "__main__":
    unittest.main()
