"""Order-path speed: no pre-sell chain read, no inter-FAK sleep, fewer polls."""

from __future__ import annotations

import ast
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import requests

from buy.book import best_ask_with_min_size, best_bid_with_min_size, book_age_s
from buy.mint_loops import chain_reconcile_action
from buy.mint_sell import is_balance_allowance_reject, tracked_sell_size
from test_mint_cpu import _END, _NOW_LIVE, _load
from test_mint_sequential import FUNDER, S, MintHarness, _cfg, _market, _prev_bag


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"


def _slice(name: str, nxt: str) -> str:
    src = MINT.read_text(encoding="utf-8")
    return src[src.find(f"def {name}") : src.find(f"\ndef {nxt}")]


class TrackedInventoryTests(unittest.TestCase):
    def test_confirmed_bag_sizes_from_mint_minus_fills(self):
        intent = {"status": "confirmed", "shares": 200.0, "sell_filled": 40.0}
        size, latch = tracked_sell_size(
            intent, 200.0, seen_key="seen_loser_inventory", tol=0.01,
        )
        self.assertEqual(latch, "has_inventory")
        self.assertAlmostEqual(size, 160.0)
        self.assertTrue(intent["seen_loser_inventory"])

    def test_full_fill_is_already_flat_without_a_chain_read(self):
        intent = {"status": "confirmed", "shares": 100.0, "sell_dump_filled": 100.0}
        size, latch = tracked_sell_size(
            intent, 100.0, seen_key="seen_dump_inventory", tol=0.01,
        )
        self.assertEqual(latch, "already_flat")
        self.assertAlmostEqual(size, 100.0)

    def test_unconfirmed_bag_waits_instead_of_looking_flat(self):
        intent = {"status": "mined", "shares": 80.0}
        _size, latch = tracked_sell_size(
            intent, 80.0, seen_key="seen_loser_inventory", tol=0.01,
        )
        self.assertEqual(latch, "await_inventory")
        self.assertFalse(intent.get("seen_loser_inventory"))

    def test_kept_leg_uses_the_keep_minus_recorded_dump(self):
        intent = {
            "status": "confirmed",
            "shares": 200.0,
            "sell_dump_kept_filled": 4.0,
        }
        size, latch = tracked_sell_size(
            intent, 25.0, seen_key="seen_kept_inventory", tol=0.01,
        )
        self.assertEqual(latch, "has_inventory")
        self.assertAlmostEqual(size, 21.0)

    def test_balance_phrase_does_not_match_other_rejects(self):
        self.assertTrue(is_balance_allowance_reject("error:not enough balance / allowance"))
        self.assertFalse(is_balance_allowance_reject("error:invalid amounts"))
        self.assertFalse(is_balance_allowance_reject("matched"))


class NoSleepAndLatencyTests(unittest.TestCase):
    def test_ladder_source_has_no_fixed_sleep_and_logs_latency(self):
        ladder = _slice("_run_fak_ladder", "_fire_loser_scrap")
        sell = _slice("_fak_sell", "_fak_buy")
        self.assertNotIn("time.sleep", ladder)
        self.assertIn("_sell_fak_with_fallback", ladder)
        self.assertIn("trigger_to_send_ms", sell)
        self.assertIn("send_to_response_ms", sell)
        self.assertNotIn("update_balance_allowance", sell)

    def test_refire_rungs_post_without_sleeping(self):
        sleeps = []
        posts = []

        def fallback(token, size, price, dry, capture, *, keep, tol):
            posts.append((round(float(size), 4), round(float(price), 4)))
            if len(posts) == 1:
                return 4.0, "matched"
            return float(size), "matched"

        ns = _load(
            "_run_fak_ladder",
            extras={
                "time": SimpleNamespace(sleep=lambda delay: sleeps.append(delay)),
                "_sell_fak_with_fallback": fallback,
                "_log_sell_book_depth": lambda **_k: None,
                "console": SimpleNamespace(print=lambda *_a, **_k: None),
                "sell_fill_vwap": lambda *_a, **_k: 0.4,
            },
        )
        sold, status, _px = ns["_run_fak_ladder"](
            "tok",
            10.0,
            [0.40, 0.36],
            dry_run=False,
            bid=0.40,
            label="dump",
            slug="slug",
            tol=0.01,
        )
        self.assertEqual(sleeps, [])
        self.assertEqual(posts, [(10.0, 0.4), (6.0, 0.36)])
        self.assertAlmostEqual(sold, 10.0)
        self.assertEqual(status, "matched")


class BalanceRejectFallbackTests(unittest.TestCase):
    def _ns(self, client, chain):
        ns = _load(
            "_fak_sell",
            "_sell_fak_with_fallback",
            "_resize_after_balance_reject",
            "_refresh_conditional_allowance",
            "_read_sell_balance",
            "_bind_sell_chain",
            "_allowance_box",
        )
        ns.update(
            time=time,
            log_event=lambda *_a, **_k: None,
            console=SimpleNamespace(print=lambda *_a, **_k: None),
            parse_sell_fill_shares=lambda result, _size: float(
                (result or {}).get("makingAmount") or 0
            ),
            is_balance_allowance_reject=is_balance_allowance_reject,
            _get_clob_client=lambda: client,
            _io_unlocked=lambda: _Null(),
        )
        ns["_bind_sell_chain"](chain, "ctf", "0xfunder")
        return ns

    def test_happy_path_does_not_refresh_allowance_or_read_chain(self):
        client = _Client(fail=None)
        chain = _BalChain(80.0)
        ns = self._ns(client, chain)
        sold, status = ns["_sell_fak_with_fallback"](
            "tok", 25.0, 0.4, False, None, keep=0.0, tol=0.01,
        )
        self.assertEqual(status, "matched")
        self.assertAlmostEqual(sold, 25.0)
        self.assertEqual(client.allowance, [])
        self.assertEqual(chain.reads, [])
        self.assertEqual(client.amounts, [25.0])

    def test_balance_reject_refreshes_once_resizes_and_retries(self):
        client = _Client(fail="not enough balance / allowance")
        chain = _BalChain(12.0)
        ns = self._ns(client, chain)
        sold, status = ns["_sell_fak_with_fallback"](
            "tok", 25.0, 0.4, False, None, keep=0.0, tol=0.01,
        )
        self.assertEqual(status, "matched")
        self.assertAlmostEqual(sold, 12.0)
        self.assertEqual(len(client.allowance), 1)
        self.assertEqual(chain.reads, ["tok"])
        self.assertEqual(client.amounts, [25.0, 12.0])

    def test_balance_at_keep_does_not_post_the_resized_retry(self):
        client = _Client(fail="not enough balance / allowance")
        chain = _BalChain(50.0)
        ns = self._ns(client, chain)
        sold, status = ns["_sell_fak_with_fallback"](
            "tok", 50.0, 0.02, False, None, keep=50.0, tol=0.01,
        )
        self.assertEqual(status, "already_flat")
        self.assertEqual(sold, 0.0)
        self.assertEqual(client.amounts, [50.0])
        self.assertEqual(chain.reads, ["tok"])
        self.assertEqual(len(client.allowance), 1)


class BookFetchTests(unittest.TestCase):
    def test_timeout_default_and_batch_then_parallel_fallback(self):
        ns = _load(
            "_book_timeout_s",
            "_parse_book_payload",
            "_fetch_book",
            "_fetch_books_parallel",
            "_fetch_books",
            extras={
                "time": time,
                "requests": requests,
                "log_event": lambda *_a, **_k: None,
                "best_bid_with_min_size": best_bid_with_min_size,
                "best_ask_with_min_size": best_ask_with_min_size,
                "book_age_s": book_age_s,
            },
        )
        self.assertAlmostEqual(ns["_book_timeout_s"](), 1.2)
        ns["_book_timeout_s"].seconds = 1.2
        ns["_book_pool"] = ThreadPoolExecutor(max_workers=2)
        posts = []
        gets = []

        class Resp:
            def __init__(self, code, body):
                self.status_code = code
                self._body = body

            def json(self):
                return self._body

        books = [
            {
                "asset_id": "up",
                "bids": [{"price": "0.40", "size": "20"}],
                "asks": [{"price": "0.42", "size": "20"}],
            },
            {
                "asset_id": "dn",
                "bids": [{"price": "0.58", "size": "20"}],
                "asks": [{"price": "0.60", "size": "20"}],
            },
        ]

        def post(url, json=None, timeout=None):
            posts.append((url, json, timeout))
            if len(posts) == 1:
                return Resp(200, books)
            return Resp(500, [])

        def get(url, params=None, timeout=None):
            gets.append((params, timeout))
            token = params["token_id"]
            body = books[0] if token == "up" else books[1]
            return Resp(200, body)

        ns["thread_session"] = lambda _slot: SimpleNamespace(post=post, get=get)
        up, dn = ns["_fetch_books"]("up", "dn", 1.0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], 1.2)
        self.assertEqual(gets, [])
        self.assertAlmostEqual(up[0], 0.40)
        self.assertAlmostEqual(dn[0], 0.58)

        up2, dn2 = ns["_fetch_books"]("up", "dn", 1.0)
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(gets), 2)
        self.assertAlmostEqual(up2[0], 0.40)
        self.assertAlmostEqual(dn2[0], 0.58)

        def timeout_post(url, json=None, timeout=None):
            raise requests.Timeout("hung")

        ns["thread_session"] = lambda _slot: SimpleNamespace(post=timeout_post, get=get)
        before = len(gets)
        empty_up, empty_dn = ns["_fetch_books"]("up", "dn", 1.0)
        self.assertEqual(len(gets), before)
        self.assertIsNone(empty_up[0])
        self.assertIsNone(empty_dn[0])


class StrategyMtimeTests(unittest.TestCase):
    def test_reload_skips_until_the_file_mtime_changes(self):
        loads = {"n": 0}

        def load():
            loads["n"] += 1
            return {"entry_enabled": loads["n"] == 1, "book_timeout_s": 1.2}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy_mint.json"
            path.write_text("{}\n", encoding="utf-8")
            ns = _load(
                "_reload_cfg",
                extras={
                    "load_strategy": load,
                    "log_event": lambda *_a, **_k: None,
                    "STRATEGY_FILE": path,
                    "_apply_book_timeout": lambda *_a, **_k: None,
                },
            )
            box = {}
            first = ns["_reload_cfg"](box)
            second = ns["_reload_cfg"](box)
            self.assertEqual(loads["n"], 1)
            self.assertIs(first, second)
            path.write_text('{"x": 1}\n', encoding="utf-8")
            stamped = path.stat().st_mtime_ns + 1_000_000
            os.utime(path, ns=(stamped, stamped))
            third = ns["_reload_cfg"](box)
            self.assertEqual(loads["n"], 2)
            self.assertIs(third["entry_enabled"], False)


class MintPollCacheTests(unittest.TestCase):
    def test_cash_wait_skips_positions_and_reuses_contract_and_balance(self):
        state = {"intents": {"prev": _prev_bag()}}
        h = MintHarness(_cfg(), state, [_market(S, 2)], balance=12.0)
        chain = _CountingChain(12.0)
        h.chain = chain
        positions = {"n": 0}

        def wrapped(funder):
            positions["n"] += 1
            return {}

        h.gateway.positions = wrapped
        self.assertEqual(h.tick(S + 1.0), "seq_wait_cash")
        self.assertEqual(positions["n"], 0)
        self.assertEqual(chain.codes, 2)
        self.assertEqual(chain.outcomes, 1)
        self.assertEqual(chain.cash, 1)
        self.assertEqual(h.tick(S + 1.5), "seq_wait_cash")
        self.assertEqual(positions["n"], 0)
        self.assertEqual(chain.codes, 2)
        self.assertEqual(chain.outcomes, 1)
        self.assertEqual(chain.cash, 1)
        self.assertEqual(h.tick(S + 5.0), "seq_wait_cash")
        self.assertEqual(chain.cash, 2)
        self.assertEqual(chain.codes, 2)

    def test_live_submit_checks_positions_once(self):
        h = MintHarness(_cfg(), {"intents": {}}, [_market(S, 2)], balance=500.0)
        positions = {"n": 0}

        def wrapped(funder):
            positions["n"] += 1
            return {}

        h.gateway.positions = wrapped
        self.assertEqual(h.tick(S), "submitted")
        self.assertEqual(positions["n"], 1)
        h.tick(S + 1.0)
        self.assertEqual(positions["n"], 1)

    def test_open_window_confirmed_bag_is_not_a_chain_query(self):
        live = {
            "status": "confirmed",
            "end_ts": _END,
            "shares": 50.0,
        }
        self.assertEqual(chain_reconcile_action(live, _NOW_LIVE), "skip")


class PrewarmOffHotPathTests(unittest.TestCase):
    def test_sell_loop_does_not_prewarm_and_reconcile_does(self):
        src = MINT.read_text(encoding="utf-8")
        sell = src[src.find("def _manage_sells_locked") : src.find("\ndef _claim_mint_intent")]
        reconcile = src[src.find("def reconcile_intents") : src.find("\ndef acquire_lock")]
        self.assertNotIn("_schedule_order_prewarm", sell)
        self.assertIn("_schedule_order_prewarm", reconcile)
        tree = ast.parse(src)
        names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertIn("_schedule_order_prewarm", names)


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *_exc):
        return False


class _BalChain:
    def __init__(self, balance: float):
        self.balance = balance
        self.reads = []

    def position_balance(self, _ctf, _owner, token):
        self.reads.append(token)
        return self.balance


class _Client:
    def __init__(self, fail):
        self.fail = fail
        self.allowance = []
        self.amounts = []

    def update_balance_allowance(self, params):
        self.allowance.append(params)

    def create_market_order(self, args):
        self.amounts.append(float(args.amount))
        return {"amount": float(args.amount)}

    def post_order(self, signed, order_type=None):
        if self.fail and len(self.amounts) == 1:
            raise RuntimeError(self.fail)
        return {"status": "matched", "makingAmount": str(signed["amount"])}


class _CountingChain:
    def __init__(self, balance: float):
        self.balance = balance
        self.codes = 0
        self.outcomes = 0
        self.cash = 0

    def has_contract(self, _addr):
        self.codes += 1
        return True

    def outcome_slot_count(self, _ctf, _cid):
        self.outcomes += 1
        return 2

    def pUSD_balance(self, _token, _owner):
        self.cash += 1
        return self.balance

    def position_balance(self, _ctf, _owner, _token):
        return 0.0

    def _rpc(self, _method, _params):
        raise RuntimeError("no rpc")


if __name__ == "__main__":
    unittest.main()
