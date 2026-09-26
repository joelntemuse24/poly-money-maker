"""CPU-path contracts: pooled HTTP, ended-bag reconcile, save-on-change."""

from __future__ import annotations

import ast
import json
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from buy.book import bid_fill_depth
from buy.chain import ChainReader, thread_session
from buy.mint_loops import (
    ENDED_CHAIN_GRACE_S,
    chain_reconcile_action,
    pending_mint_reserve,
    persist_digest,
    state_persist_changed,
)
from buy.oracle_log import fetch_crypto_price
import buy.mint_sell as mint_sell


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


def _load(*names: str, extras: dict | None = None) -> dict:
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    if names:
        want = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        found = {node.name for node in want}
        missing = set(names) - found
        if missing:
            raise AssertionError(f"missing {sorted(missing)}")
    else:
        want = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    ns: dict = {
        "time": time,
        "json": json,
        "os": __import__("os"),
        "Path": Path,
        "threading": threading,
        "persist_digest": __import__("buy.mint_loops", fromlist=["persist_digest"]).persist_digest,
        "contextmanager": contextmanager,
    }
    if extras:
        ns.update(extras)
    exec(compile(ast.Module(body=want, type_ignores=[]), str(MINT), "exec"), ns)
    return ns


def _fn(name: str, extras: dict | None = None):
    ns = _load(name, extras=extras)
    return ns[name]


def _sell_runtime(saves: list) -> dict:
    """Namespace that can run ``_manage_sells_locked`` without mintbot import."""
    ns = _load()
    for name in dir(mint_sell):
        if name.startswith("_"):
            continue
        ns.setdefault(name, getattr(mint_sell, name))
    ns["bid_fill_depth"] = bid_fill_depth
    ns["log_event"] = lambda *_a, **_k: None
    ns["notify"] = lambda *_a, **_k: None
    ns["console"] = SimpleNamespace(print=lambda *_a, **_k: None)
    ns["STATE_LOCK"] = threading.RLock()
    ns["STATE_FILE"] = Path("/tmp/positions_mint_cpu_test.json")

    @contextmanager
    def _io_unlocked():
        yield

    ns["_io_unlocked"] = _io_unlocked
    ns["_oracle_bag_view"] = lambda _cid: SimpleNamespace(twap=None, open_usd=None, obs_ts=None)
    real_remember = ns["remember_persisted_state"]

    def atomic_save(path, payload):
        saves.append(json.loads(json.dumps(payload)))
        real_remember(payload)

    ns["atomic_save"] = atomic_save
    # commit_state closed over the name atomic_save at runtime via global lookup.
    return ns


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

    def test_digest_tracks_live_changes_and_skips_terminal_fields(self):
        live = {
            "status": "confirmed",
            "shares": 50.0,
            "sell_limit": 0.02,
            "last_up_bid": 0.01,
            "updated_at": 1.0,
        }
        done = {"status": "completed", "shares": 50.0, "slug": "old", "note": "audit"}
        state = {"intents": {"live": dict(live), "done": dict(done)}, "note": "desk"}
        base = persist_digest(state)

        state["intents"]["live"]["last_up_bid"] = 0.4
        state["intents"]["live"]["updated_at"] = 9.0
        self.assertEqual(persist_digest(state), base)

        state["intents"]["live"]["sell_limit"] = 0.01
        self.assertNotEqual(persist_digest(state), base)
        state["intents"]["live"]["sell_limit"] = 0.02
        self.assertEqual(persist_digest(state), base)

        state["intents"]["fresh"] = {"status": "submitting", "shares": 50.0}
        self.assertNotEqual(persist_digest(state), base)
        del state["intents"]["fresh"]
        self.assertEqual(persist_digest(state), base)

        state["intents"]["live"]["status"] = "completed"
        self.assertNotEqual(persist_digest(state), base)
        state["intents"]["live"]["status"] = "confirmed"
        self.assertEqual(persist_digest(state), base)

        state["extra"] = 1
        self.assertNotEqual(persist_digest(state), base)

    def test_digest_does_not_read_terminal_intent_fields(self):
        class Guard(dict):
            def __getitem__(self, key):
                if key != "status":
                    raise AssertionError(key)
                return super().__getitem__(key)

            def get(self, key, default=None):
                if key != "status":
                    raise AssertionError(key)
                return super().get(key, default)

            def items(self):
                raise AssertionError("items")

            def __iter__(self):
                raise AssertionError("iter")

        intents = {}
        for i in range(3000):
            intents[f"c{i}"] = Guard(status="completed", shares=50.0, blob="x" * 80)
        intents["live"] = {"status": "confirmed", "shares": 50.0, "sell_limit": 0.02}
        state = {"intents": intents}
        started = time.perf_counter()
        first = persist_digest(state)
        second = persist_digest(state)
        elapsed = time.perf_counter() - started
        self.assertEqual(first, second)
        self.assertLess(elapsed, 0.05)
        intents["live"]["sell_filled"] = 1.0
        self.assertNotEqual(persist_digest(state), first)

    def test_sell_loop_skips_bid_only_and_saves_real_changes(self):
        saves: list = []
        ns = _sell_runtime(saves)
        now = 1_700_000_100.0
        ns["time"] = SimpleNamespace(time=lambda: now)
        books = {"px": (0.40, 10.0, [])}

        def fetch_books(_up, _dn, _min_size):
            px, sz, levels = books["px"]
            return (px, sz, levels), (0.60, sz, levels)

        ns["_fetch_books"] = fetch_books
        intent = {
            "status": "confirmed",
            "end_ts": now + 400.0,
            "start_ts": now - 500.0,
            "up_token": "up",
            "dn_token": "dn",
            "shares": 50.0,
            "slug": "btc-updown-15m-test",
        }
        state = {"intents": {"cid": intent}}
        cfg = {
            "sell_threshold": 0.02,
            "sell_floor": 0.02,
            "sell_opposite_min": 0.90,
            "sell_persist_s": 5.0,
            "sell_persist_last_min_s": 2.0,
            "sell_persist_last_min_window_s": 60.0,
            "sell_persist_skip_ttm_s": 90.0,
            "sell_persist_skip_when_sized": False,
            "sell_fak_px": 0.02,
            "sell_scrap_blind_enabled": False,
            "sell_scrap_blind_px": 0.01,
            "sell_scrap_blind_backoff_s": 3.0,
            "sell_scrap_rest_enabled": False,
            "sell_scrap_rest_px": 0.02,
            "sell_scrap_rest_min_ahead_s": 180.0,
            "sell_cooldown_s": 3.0,
            "sell_winner_min": 0.999,
            "sell_clob_max_price": 0.99,
            "sell_clob_min_price": 0.01,
            "sell_min_bid_size": 1.0,
            "position_tolerance": 0.01,
            "dry_run": True,
            "shares": 50.0,
            "sell_dump_enabled": False,
            "sell_late_window_s": 0.0,
            "ctf_address": "0xctf",
        }
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertEqual(len(saves), 1)
        saves.clear()
        books["px"] = (0.41, 12.0, [])
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertEqual(saves, [])
        books["px"] = (0.42, 8.0, [])

        def fetch_and_fill(_up, _dn, _min_size):
            intent["sell_limit"] = 0.02
            intent["sold_loser"] = True
            return (0.42, 8.0, []), (0.58, 8.0, [])

        ns["_fetch_books"] = fetch_and_fill
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertEqual(len(saves), 1)
        self.assertTrue(saves[-1]["intents"]["cid"]["sold_loser"])
        self.assertEqual(saves[-1]["intents"]["cid"]["sell_limit"], 0.02)

    def test_raised_tick_is_saved_on_the_next_tick(self):
        saves: list = []
        ns = _sell_runtime(saves)
        now = 1_700_000_100.0
        ns["time"] = SimpleNamespace(time=lambda: now)
        intent = {
            "status": "confirmed",
            "end_ts": now + 400.0,
            "start_ts": now - 500.0,
            "up_token": "up",
            "dn_token": "dn",
            "shares": 50.0,
            "slug": "btc-updown-15m-test",
        }
        state = {"intents": {"cid": intent}}
        cfg = {
            "sell_threshold": 0.02,
            "sell_floor": 0.02,
            "sell_opposite_min": 0.90,
            "sell_persist_s": 5.0,
            "sell_persist_last_min_s": 2.0,
            "sell_persist_last_min_window_s": 60.0,
            "sell_persist_skip_ttm_s": 90.0,
            "sell_persist_skip_when_sized": False,
            "sell_fak_px": 0.02,
            "sell_scrap_blind_enabled": False,
            "sell_scrap_blind_px": 0.01,
            "sell_scrap_blind_backoff_s": 3.0,
            "sell_scrap_rest_enabled": False,
            "sell_scrap_rest_px": 0.02,
            "sell_scrap_rest_min_ahead_s": 180.0,
            "sell_cooldown_s": 3.0,
            "sell_winner_min": 0.999,
            "sell_clob_max_price": 0.99,
            "sell_clob_min_price": 0.01,
            "sell_min_bid_size": 1.0,
            "position_tolerance": 0.01,
            "dry_run": True,
            "shares": 50.0,
            "sell_dump_enabled": False,
            "sell_late_window_s": 0.0,
            "ctf_address": "0xctf",
        }

        def quiet(_up, _dn, _min_size):
            return (0.40, 10.0, []), (0.60, 10.0, [])

        ns["_fetch_books"] = quiet
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())
        saves.clear()

        def mutate_then_raise(_up, _dn, _min_size):
            intent["sell_limit"] = 0.02
            intent["sell_filled"] = 50.0
            raise RuntimeError("tick blew up after the fill")

        ns["_fetch_books"] = mutate_then_raise
        with self.assertRaises(RuntimeError):
            ns["_manage_sells_locked"](cfg, state, object())
        self.assertEqual(saves, [])
        self.assertEqual(intent["sell_limit"], 0.02)
        ns["_fetch_books"] = quiet
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertEqual(len(saves), 1)
        saved = saves[-1]["intents"]["cid"]
        self.assertEqual(saved["sell_limit"], 0.02)
        self.assertEqual(saved["sell_filled"], 50.0)

    def test_sell_and_mint_loops_save_against_the_last_success(self):
        src = MINT.read_text()
        manage = src[src.find("def _manage_sells_locked") : src.find("\ndef _claim_mint_intent")]
        mint = src[src.find("def run_mint_cycle") : src.find("\ndef _reload_cfg")]
        save = src[src.find("def atomic_save") : src.find("def remember_persisted_state")]
        self.assertIn("commit_state(state, dirty=dirty)", manage)
        self.assertNotIn("snapshot_persist_intents", manage)
        self.assertIn("commit_state(state)", mint)
        self.assertNotIn("snapshot_persist_intents", mint)
        self.assertIn("remember_persisted_state(payload)", save)
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


def _dump_cfg(**extra) -> dict:
    cfg = {
        "sell_threshold": 0.02,
        "sell_floor": 0.02,
        "sell_opposite_min": 0.90,
        "sell_persist_s": 5.0,
        "sell_persist_last_min_s": 2.0,
        "sell_persist_last_min_window_s": 60.0,
        "sell_persist_skip_when_sized": False,
        "sell_fak_px": 0.02,
        "sell_scrap_blind_enabled": False,
        "sell_scrap_blind_px": 0.01,
        "sell_scrap_blind_backoff_s": 3.0,
        "sell_scrap_rest_enabled": False,
        "sell_scrap_rest_px": 0.02,
        "sell_scrap_rest_min_ahead_s": 180.0,
        "sell_cooldown_s": 3.0,
        "sell_winner_min": 0.999,
        "sell_clob_max_price": 0.99,
        "sell_clob_min_price": 0.01,
        "sell_min_bid_size": 1.0,
        "position_tolerance": 0.01,
        "dry_run": False,
        "shares": 50.0,
        "sell_dump_enabled": True,
        "sell_dump_below": 0.60,
        "sell_dump_persist_s": 2.0,
        "sell_dump_fak_retries": 2,
        "sell_dump_ladder_step": 0.04,
        "sell_dump_ladder_rungs": 4,
        "sell_dump_max_ttm_s": 240.0,
        "sell_late_window_s": 0.0,
        "ctf_address": "",
    }
    cfg.update(extra)
    return cfg


def _dump_harness(fak_sell):
    """Sell loop with a fixed 0.51/0.49 book and a stub FAK."""
    events: list = []
    fak_calls: list = []
    clock = {"now": 1_700_000_000.0}
    saves: list = []
    ns = _sell_runtime(saves)
    ns["time"] = SimpleNamespace(
        time=lambda: clock["now"],
        sleep=lambda *_a, **_k: None,
    )
    ns["log_event"] = lambda event, **kwargs: events.append({"event": event, **kwargs})
    book = {
        "up": (0.51, 50.0, [{"price": "0.51", "size": "80"}]),
        "dn": (0.49, 50.0, [{"price": "0.49", "size": "80"}]),
    }

    def fetch_books(_up, _dn, _min_size):
        return book["up"], book["dn"]

    def wrapped_fak(token_id, size, price, dry_run, capture=None):
        fak_calls.append(
            {
                "token_id": token_id,
                "size": float(size),
                "price": float(price),
                "dry_run": dry_run,
            }
        )
        return fak_sell(token_id, size, price, dry_run)

    ns["_fetch_books"] = fetch_books
    ns["_fak_sell"] = wrapped_fak
    ns["_fetch_book"] = lambda *_a, **_k: (
        0.48,
        50.0,
        [{"price": "0.48", "size": "80"}],
    )
    return ns, events, fak_calls, clock


def _held_after_scrap(end_ts: float, **extra) -> dict:
    row = {
        "status": "confirmed",
        "end_ts": end_ts,
        "start_ts": end_ts - 900.0,
        "up_token": "up-tok",
        "dn_token": "dn-tok",
        "shares": 50.0,
        "slug": "btc-updown-15m-test",
        "sold_loser": True,
        "sold_leg": "dn",
        "sold_loser_at": end_ts - 500.0,
    }
    row.update(extra)
    return row


def _fill_fak(_token, size, _price, _dry):
    return float(size), "matched"


class DumpMaxTtmGateTests(unittest.TestCase):
    def _tick(self, ns, cfg, intent):
        state = {"intents": {"cid-dump": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())
        return intent

    def test_dump_blocked_at_ttm_427_with_bid_0_51(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 427.0
        intent = _held_after_scrap(
            end, sell_dump_armed_at=clock["now"] - 5.0,
        )
        self._tick(ns, _dump_cfg(), intent)
        self.assertEqual(fak_calls, [])
        self.assertIsNone(intent.get("sell_dump_armed_at"))
        self.assertFalse(intent.get("sold_dump"))
        gated = [row for row in events if row["event"] == "sell_dump_time_gated"]
        self.assertEqual(len(gated), 1)
        self.assertEqual(gated[0]["condition_id"], "cid-dump")
        self.assertEqual(gated[0]["slug"], "btc-updown-15m-test")
        self.assertEqual(gated[0]["leg"], "up")
        self.assertAlmostEqual(gated[0]["bid"], 0.51)
        self.assertAlmostEqual(gated[0]["ttm"], 427.0)
        self.assertAlmostEqual(gated[0]["cutoff"], 240.0)
        self.assertFalse(any(row["event"] == "sell_dump_persist" for row in events))

        clock["now"] += 1.0
        self._tick(ns, _dump_cfg(), intent)
        gated = [row for row in events if row["event"] == "sell_dump_time_gated"]
        self.assertEqual(len(gated), 1)
        self.assertEqual(fak_calls, [])

        clock["now"] += 14.0
        self._tick(ns, _dump_cfg(), intent)
        gated = [row for row in events if row["event"] == "sell_dump_time_gated"]
        self.assertEqual(len(gated), 2)
        self.assertAlmostEqual(gated[1]["ttm"], 412.0)
        self.assertEqual(fak_calls, [])

    def test_dump_fires_at_ttm_200_after_2s_persist(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end)
        cfg = _dump_cfg()
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        self.assertEqual(intent.get("sell_dump_armed_at"), clock["now"])
        self.assertFalse(intent.get("sold_dump"))
        self.assertFalse(any(row["event"] == "sell_dump_time_gated" for row in events))

        clock["now"] += 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.51)
        self.assertEqual(fak_calls[0]["token_id"], "up-tok")
        self.assertAlmostEqual(fak_calls[0]["size"], 50.0)
        self.assertTrue(intent.get("sold_dump"))
        self.assertEqual(intent.get("sell_dump_leg"), "up")

    def test_persist_started_outside_240_resets_and_waits_inside(self):
        ns, _events, fak_calls, clock = _dump_harness(_fill_fak)
        end = 1_700_000_242.0
        clock["now"] = end - 242.0
        stale_arm = clock["now"] - 30.0
        intent = _held_after_scrap(end, sell_dump_armed_at=stale_arm)
        cfg = _dump_cfg()
        self._tick(ns, cfg, intent)
        self.assertIsNone(intent.get("sell_dump_armed_at"))
        self.assertEqual(fak_calls, [])

        clock["now"] = end - 240.0
        self._tick(ns, cfg, intent)
        self.assertEqual(intent.get("sell_dump_armed_at"), clock["now"])
        self.assertNotEqual(intent.get("sell_dump_armed_at"), stale_arm)
        self.assertEqual(fak_calls, [])

        armed = intent["sell_dump_armed_at"]
        clock["now"] = armed + 1.9
        self._tick(ns, cfg, intent)
        self.assertEqual(intent.get("sell_dump_armed_at"), armed)
        self.assertEqual(fak_calls, [])

        clock["now"] = armed + 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.51)
        self.assertTrue(intent.get("sold_dump"))
        self.assertAlmostEqual(end - (armed + 2.0), 238.0)

    def test_gate_zero_keeps_the_old_dump_at_ttm_427(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 427.0
        intent = _held_after_scrap(end)
        cfg = _dump_cfg(sell_dump_max_ttm_s=0)
        self._tick(ns, cfg, intent)
        self.assertEqual(fak_calls, [])
        self.assertEqual(intent.get("sell_dump_armed_at"), clock["now"])
        self.assertFalse(any(row["event"] == "sell_dump_time_gated" for row in events))

        clock["now"] += 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertAlmostEqual(fak_calls[0]["price"], 0.51)
        self.assertTrue(intent.get("sold_dump"))

    def test_missing_gate_key_keeps_the_old_dump(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 427.0
        intent = _held_after_scrap(end)
        cfg = _dump_cfg()
        del cfg["sell_dump_max_ttm_s"]
        self._tick(ns, cfg, intent)
        self.assertEqual(intent.get("sell_dump_armed_at"), clock["now"])
        clock["now"] += 2.0
        self._tick(ns, cfg, intent)
        self.assertEqual(len(fak_calls), 1)
        self.assertTrue(intent.get("sold_dump"))
        self.assertFalse(any(row["event"] == "sell_dump_time_gated" for row in events))

    def test_ladder_retries_after_fire_are_not_time_gated(self):
        misses = {"n": 0}

        def fak_sell(_token, size, _price, _dry):
            misses["n"] += 1
            if misses["n"] == 1:
                return 0.0, "error:no orders found to match with FAK order"
            return float(size), "matched"

        ns, events, fak_calls, clock = _dump_harness(fak_sell)
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
        self._tick(ns, _dump_cfg(), intent)
        self.assertEqual([row["price"] for row in fak_calls], [0.51, 0.48])
        self.assertTrue(intent.get("sold_dump"))
        self.assertFalse(any(row["event"] == "sell_dump_time_gated" for row in events))
        src = MINT.read_text()
        refire = src[
            src.find("def _run_dump_fak_with_refire") : src.find(
                "\ndef _apply_sell_fire_cancel"
            )
        ]
        self.assertNotIn("sell_dump_max_ttm_s", refire)
        self.assertNotIn("dump_time_gate_open", refire)


if __name__ == "__main__":
    unittest.main()
