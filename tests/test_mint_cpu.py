"""CPU-path contracts: pooled HTTP, ended-bag reconcile, save-on-change."""

from __future__ import annotations

import ast
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy.chain import ChainReader, thread_session
from buy.mint_loops import (
    ENDED_CHAIN_GRACE_S,
    chain_reconcile_action,
    pending_mint_reserve,
    state_persist_changed,
)
from buy.oracle_log import fetch_crypto_price


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"

_ACTIVE = frozenset(
    {
        "submitting",
        "pending",
        "executed",
        "mined",
        "confirmed_waiting_inventory",
        "confirmed",
    }
)
_END = 1_700_000_000.0
_NOW_LIVE = _END - 60.0
_NOW_GRACE = _END + 30.0
_NOW_FINAL = _END + ENDED_CHAIN_GRACE_S + 5.0
_NOW_CAP = _END + 121.0


def _fn(name: str, extras: dict | None = None):
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    want = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            want = node
            break
    if want is None:
        raise AssertionError(f"{name} not found")
    ns: dict = {"time": time}
    if extras:
        ns.update(extras)
    exec(compile(ast.Module(body=[want], type_ignores=[]), str(MINT), "exec"), ns)
    return ns[name]


def _bag(**extra) -> dict:
    row = {
        "status": "confirmed",
        "shares": 50.0,
        "before_up": 0.0,
        "before_dn": 0.0,
        "up_token": "up-tok",
        "dn_token": "dn-tok",
        "end_ts": _END,
        "start_ts": _END - 900.0,
    }
    row.update(extra)
    return row


class SessionReuseTests(unittest.TestCase):
    def test_thread_session_is_one_per_thread_and_slot(self):
        main_book = thread_session("clob_book")
        self.assertIs(thread_session("clob_book"), main_book)
        self.assertIsNot(thread_session("relayer"), main_book)
        other: dict = {}

        def worker() -> None:
            other["a"] = thread_session("clob_book")
            other["b"] = thread_session("clob_book")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertIs(other["a"], other["b"])
        self.assertIsNot(other["a"], main_book)

    def test_chain_reader_reuses_its_session_and_timeout(self):
        reader = ChainReader("https://polygon.example/rpc", timeout=15.0)
        other = ChainReader("https://polygon.example/rpc", timeout=15.0)
        self.assertIsNot(reader.session, other.session)
        posts: list = []

        class Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"result": "0x1"}

        def post(url, json=None, timeout=None):
            posts.append((url, timeout, json["method"]))
            return Resp()

        reader.session.post = post
        reader._rpc("eth_blockNumber", [])
        reader._rpc("eth_chainId", [])
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][0], "https://polygon.example/rpc")
        self.assertEqual(posts[0][1], 15.0)
        self.assertEqual(posts[1][1], 15.0)
        self.assertEqual([row[2] for row in posts], ["eth_blockNumber", "eth_chainId"])

    def test_oracle_price_reuses_the_crypto_price_session(self):
        session = thread_session("crypto_price")
        calls: list = []

        class Resp:
            text = (
                '{"openPrice":"1","closePrice":null,'
                '"completed":false,"incomplete":true}'
            )

            def raise_for_status(self) -> None:
                return None

        def get(url, params=None, timeout=None, headers=None):
            calls.append((timeout, params["variant"] if params else None))
            return Resp()

        session.get = get
        try:
            fetch_crypto_price(1_700_000_000)
            fetch_crypto_price(1_700_000_000)
        finally:
            del session.get
        self.assertEqual(calls, [(3.0, "fifteen"), (3.0, "fifteen")])

    def test_book_and_relayer_status_use_sessions_submit_stays_direct(self):
        src = MINT.read_text()
        book = src[src.find("def _fetch_book") : src.find("def _fetch_books")]
        relayer = src[src.find("def get_relayer_transaction") : src.find("def reconcile_intents")]
        submit = src[src.find("def submit_mint_batch") : src.find("def get_relayer_transaction")]
        self.assertIn('thread_session("clob_book").get', book)
        self.assertNotIn("requests.get", book)
        self.assertIn("timeout=5", book)
        self.assertIn('thread_session("relayer").get', relayer)
        self.assertIn("timeout=15", relayer)
        self.assertIn("requests.get", submit)
        self.assertIn("requests.post", submit)


class EndedReconcileTests(unittest.TestCase):
    def test_action_skips_ended_confirmed_until_one_final_read(self):
        live = _bag(end_ts=_END)
        self.assertEqual(chain_reconcile_action(live, _NOW_LIVE), "query")
        self.assertEqual(chain_reconcile_action(live, _NOW_GRACE), "skip")
        self.assertEqual(chain_reconcile_action(live, _NOW_FINAL), "final")
        done = _bag(chain_reconcile_done=True)
        self.assertEqual(chain_reconcile_action(done, _NOW_FINAL), "skip")
        self.assertEqual(chain_reconcile_action(_bag(status="completed"), _NOW_FINAL), "skip")
        self.assertEqual(chain_reconcile_action(_bag(status="failed"), _NOW_FINAL), "skip")

    def test_inflight_keeps_querying_after_the_market_ends(self):
        for status in ("submitting", "pending", "executed", "mined", "confirmed_waiting_inventory"):
            intent = _bag(status=status, chain_reconcile_done=True)
            self.assertEqual(chain_reconcile_action(intent, _NOW_FINAL), "query", status)

    def _reconcile(self):
        return _fn(
            "reconcile_intents",
            {
                "STATE_LOCK": threading.RLock(),
                "chain_reconcile_action": chain_reconcile_action,
                "get_relayer_transaction": lambda *_a, **_k: None,
                "relayer_error_detail": lambda _rec: ("", ""),
                "mark_intent_failed": lambda *_a, **_k: None,
                "log_event": lambda *_a, **_k: None,
                "notify": lambda *_a, **_k: None,
                "console": SimpleNamespace(print=lambda *_a, **_k: None),
            },
        )

    def test_reconcile_does_not_chain_query_ended_confirmed_bags(self):
        class Chain:
            def __init__(self) -> None:
                self.calls: list = []

            def position_balance(self, ctf, funder, token):
                self.calls.append(token)
                return 50.0

        cfg = {
            "relayer_url": "https://relayer.example",
            "ctf_address": "0xctf",
            "position_tolerance": 0.01,
        }
        reconcile = self._reconcile()
        grace = {
            "intents": {
                "old": _bag(),
                "live": _bag(end_ts=_NOW_GRACE + 900.0, up_token="live-up", dn_token="live-dn"),
            }
        }
        chain = Chain()
        reconcile(grace, cfg, chain, "0xfunder", _NOW_GRACE)
        self.assertEqual(chain.calls, ["live-up", "live-dn"])
        self.assertNotIn("chain_reconcile_done", grace["intents"]["old"])

        final_state = {"intents": {"old": _bag()}}
        chain = Chain()
        reconcile(final_state, cfg, chain, "0xfunder", _NOW_FINAL)
        self.assertEqual(chain.calls, ["up-tok", "dn-tok"])
        self.assertTrue(final_state["intents"]["old"]["chain_reconcile_done"])
        chain.calls.clear()
        reconcile(final_state, cfg, chain, "0xfunder", _NOW_FINAL + 30.0)
        self.assertEqual(chain.calls, [])

        hot = {"intents": {"live": _bag(end_ts=_NOW_LIVE + 900.0)}}
        chain = Chain()
        reconcile(hot, cfg, chain, "0xfunder", _NOW_LIVE, skip_confirmed_inventory=True)
        self.assertEqual(chain.calls, [])

    def test_final_read_marks_empty_inventory_completed_once(self):
        class Chain:
            def position_balance(self, ctf, funder, token):
                return 0.0

        reconcile = self._reconcile()
        state = {"intents": {"old": _bag()}}
        cfg = {
            "relayer_url": "https://relayer.example",
            "ctf_address": "0xctf",
            "position_tolerance": 0.01,
        }
        reconcile(state, cfg, Chain(), "0xfunder", _NOW_FINAL)
        self.assertEqual(state["intents"]["old"]["status"], "completed")
        self.assertTrue(state["intents"]["old"]["chain_reconcile_done"])
        self.assertEqual(chain_reconcile_action(state["intents"]["old"], _NOW_FINAL + 10), "skip")


class SaveOnChangeTests(unittest.TestCase):
    def test_bids_and_updated_at_alone_do_not_count(self):
        before = {
            "cid": {
                "status": "confirmed",
                "last_up_bid": 0.01,
                "last_dn_bid": 0.99,
                "updated_at": 1.0,
            }
        }
        after = {
            "cid": {
                "status": "confirmed",
                "last_up_bid": 0.02,
                "last_dn_bid": 0.98,
                "last_up_bid_size": 20.0,
                "last_dn_bid_size": 20.0,
                "updated_at": 2.0,
            }
        }
        self.assertFalse(state_persist_changed(before, after))
        changed = {
            "cid": {
                "status": "confirmed",
                "sold_loser": True,
                "last_up_bid": 0.02,
                "updated_at": 3.0,
            }
        }
        self.assertTrue(state_persist_changed(before, changed))
        self.assertTrue(state_persist_changed(before, {}))

    def test_sell_and_mint_loops_save_only_when_persist_view_changes(self):
        src = MINT.read_text()
        manage = src[src.find("def _manage_sells_locked") : src.find("\ndef _claim_mint_intent")]
        mint = src[src.find("def run_mint_cycle") : src.find("\ndef _reload_cfg")]
        self.assertIn("snapshot_persist_intents", manage)
        self.assertIn("state_persist_changed", manage)
        self.assertNotIn('updated_at"] = now\n        dirty = True', manage)
        self.assertIn("state_persist_changed", mint)
        self.assertIn("atomic_save(STATE_FILE, state)", mint)
        save = src[src.find("def atomic_save") : src.find("def load_state")]
        self.assertNotIn("indent=2", save)
        self.assertIn('separators=(",", ":")', save)
        self.assertIn("sort_keys=True", save)
        self.assertIn("os.fsync", save)


class OpenBagAndReserveTests(unittest.TestCase):
    def test_ended_confirmed_does_not_fill_slots_or_reserve_cash(self):
        count = _fn("open_intent_count", {"ACTIVE_STATUSES": _ACTIVE})
        slots = _fn("mint_slots_full", {"ACTIVE_STATUSES": _ACTIVE})
        ended = _bag()
        live = _bag(end_ts=_NOW_CAP + 800.0, start_ts=_NOW_CAP)
        state = {"intents": {"old": ended, "live": live}}
        self.assertEqual(count(state, _NOW_CAP), 1)
        self.assertEqual(count({"intents": {"old": ended}}, _NOW_CAP), 0)
        cfg = {"max_open_sets": 1}
        self.assertFalse(slots({"intents": {"old": ended}}, cfg, _NOW_CAP, _NOW_CAP + 1000.0))
        self.assertTrue(slots(state, cfg, _NOW_CAP, _NOW_CAP + 5000.0))
        self.assertEqual(pending_mint_reserve(state), 0.0)
        inflight = {"intents": {"stuck": _bag(status="mined", shares=50.0)}}
        self.assertEqual(pending_mint_reserve(inflight), 50.0)
        self.assertEqual(pending_mint_reserve({"intents": {"old": _bag(status="completed")}}), 0.0)


if __name__ == "__main__":
    unittest.main()
