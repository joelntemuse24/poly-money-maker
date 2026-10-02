"""Sequential bags: one bag of capital. No mintbot import, no RPC, no relayer."""

from __future__ import annotations

import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

import buy.mint_loops as mint_loops
import buy.mint_sequence as mint_sequence
from buy.contracts import build_atomic_mint_calls
from buy.mint_gas import mint_gas_settings
from buy.mint_sequence import (
    SeqWaits,
    seq_busy_bag,
    seq_eligible_markets,
    seq_late_markets,
    seq_phase,
    seq_settings,
    validate_seq,
)
from test_mint_cpu import _held_after_scrap, _dump_cfg, _dump_harness, _fill_fak, _load
from test_mint_cpu import _open_scrap_bag, _scrap_cfg, _scrap_harness

ACTIVE = frozenset(
    {"submitting", "pending", "executed", "mined", "confirmed_waiting_inventory", "confirmed"}
)
S = 1_800_000_000.0  # window N opens; window N-1 is [S-900, S]
FUNDER = "0x" + "11" * 20


def _cid(n: int) -> str:
    return "0x" + f"{n:02x}" * 32


def _market(start: float, n: int, **extra):
    row = dict(
        condition_id=_cid(n),
        slug=f"btc-updown-15m-{int(start)}",
        question="BTC up or down",
        series_slug="btc-up-or-down-15m",
        start_ts=start,
        end_ts=start + 900.0,
        up_token=str(1000 + n),
        dn_token=str(2000 + n),
        active=True,
        closed=False,
        accepting_orders=True,
        neg_risk=False,
    )
    row.update(extra)
    market = SimpleNamespace(**row)
    market.minutes_to_start = lambda now=None, _m=market: (_m.start_ts - float(now)) / 60.0
    return market


def _prev_bag(**extra) -> dict:
    row = {
        "status": "confirmed",
        "condition_id": _cid(1),
        "slug": "btc-updown-15m-prev",
        "start_ts": S - 900.0,
        "end_ts": S,
        "up_token": "1001",
        "dn_token": "2001",
        "shares": 200.0,
        "dry_run": False,
    }
    row.update(extra)
    return row


def _cfg(**extra) -> dict:
    cfg = {
        "entry_enabled": True,
        "dry_run": False,
        "shares": 200.0,
        "enter_min_ttm_min": 0.0,
        "enter_max_ttm_min": 45.0,
        "series_slugs": ["btc-up-or-down-15m"],
        "one_entry_per_market": True,
        "max_open_sets": 1,
        "count_kept_loser_as_open": False,
        "mint_fail_cooldown_s": 30.0,
        "mint_submitting_timeout_s": 90.0,
        "mint_max_attempts": 3,
        "position_tolerance": 0.01,
        "require_accepting_orders": True,
        "pUSD_address": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
        "ctf_address": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
        "standard_adapter_address": "0xAdA100Db00Ca00073811820692005400218FcE1f",
        "mint_sequential": True,
        "mint_seq_lead_s": 30.0,
        "mint_seq_cutoff_s": 240.0,
    }
    cfg.update(extra)
    return cfg


class FakeChain:
    def __init__(self, balance: float):
        self.balance = balance

    def has_contract(self, _addr):
        return True

    def outcome_slot_count(self, _ctf, _cid):
        return 2

    def pUSD_balance(self, _token, _owner):
        return self.balance

    def position_balance(self, _ctf, _owner, _token):
        return 0.0

    def _rpc(self, _method, _params):
        raise RuntimeError("no rpc in tests")


class FakeGateway:
    def __init__(self, markets):
        self.markets = markets

    def discover(self, _slugs):
        return list(self.markets)

    def positions(self, _funder):
        return {}


class MintHarness:
    """Runs the real ``run_mint_cycle`` with chain, relayer, and disk stubbed."""

    def __init__(self, cfg: dict, state: dict, markets: list, balance: float):
        self.clock = {"now": S}
        self.events: list = []
        self.submits: list = []
        self.saves = 0
        self.cfg = cfg
        self.state = state
        self.chain = FakeChain(balance)
        self.gateway = FakeGateway(markets)
        ns = _load(
            "run_mint_cycle",
            "_claim_mint_intent",
            "_log_seq_skips",
            "already_minted",
            "mint_slots_full",
            "open_intent_count",
            "mark_intent_failed",
            "fail_stale_submitting_intents",
            "eligible_markets",
        )
        for module in (mint_loops, mint_sequence):
            for name in dir(module):
                if not name.startswith("_"):
                    ns.setdefault(name, getattr(module, name))
        ns.update(
            time=SimpleNamespace(time=lambda: self.clock["now"]),
            os=SimpleNamespace(getenv=lambda key, default=None: FUNDER if key == "FUNDER_ADDRESS" else default),
            STOP_FILE=SimpleNamespace(exists=lambda: False),
            STATE_FILE=None,
            STATE_LOCK=threading.RLock(),
            RELAY_SUBMIT_LOCK=threading.Lock(),
            ACTIVE_STATUSES=ACTIVE,
            _SEQ_WAITS=SeqWaits(),
            _intent_store=None,
            to_checksum_address=lambda addr: addr,
            skip_mint_discovery_for_sell=lambda *_a, **_k: False,
            reconcile_intents=lambda *_a, **_k: None,
            commit_state=lambda *_a, **_k: False,
            atomic_save=self._save,
            write_loop_heartbeat=lambda *_a, **_k: None,
            log_event=lambda event, **kw: self.events.append({"event": event, **kw}),
            notify=lambda *_a, **_k: None,
            console=SimpleNamespace(print=lambda *_a, **_k: None),
            Panel=lambda *_a, **_k: None,
            box=SimpleNamespace(HEAVY=None),
            build_atomic_mint_calls=build_atomic_mint_calls,
            mint_gas_settings=mint_gas_settings,
            submit_mint_batch=self._submit,
            nullcontext=nullcontext,
        )
        self.ns = ns

    def _save(self, _path, _payload):
        self.saves += 1

    def _submit(self, calls, metadata, **_kw):
        self.submits.append(metadata)
        return f"tx-{len(self.submits)}", None, {"gas_limit": 600_000}

    def tick(self, at: float) -> str:
        self.clock["now"] = at
        return self.ns["run_mint_cycle"](self.cfg, self.state, self.gateway, self.chain)

    def named(self, name):
        return [row for row in self.events if row["event"] == name]


class SequencePureTests(unittest.TestCase):
    def test_off_by_default_and_validation(self):
        self.assertEqual(seq_settings({}), (False, 30.0, 240.0))
        validate_seq({})
        validate_seq(_cfg())
        for key, bad in (("mint_seq_lead_s", -1), ("mint_seq_cutoff_s", -5), ("mint_seq_cutoff_s", 600)):
            with self.assertRaises(ValueError, msg=key):
                validate_seq(_cfg(**{key: bad}))

    def test_phase_boundaries(self):
        self.assertEqual(seq_phase(S - 31, S, 30, 240), "early")
        self.assertEqual(seq_phase(S - 30, S, 30, 240), "open")
        self.assertEqual(seq_phase(S + 240, S, 30, 240), "open")
        self.assertEqual(seq_phase(S + 241, S, 30, 240), "late")

    def test_eligible_window_is_lead_to_cutoff_and_soonest_first(self):
        now_market = _market(S, 2)
        next_market = _market(S + 900.0, 3)
        markets = [next_market, now_market]
        self.assertEqual(seq_eligible_markets(markets, _cfg(), S - 31), [])
        self.assertEqual(seq_eligible_markets(markets, _cfg(), S - 30), [now_market])
        self.assertEqual(seq_eligible_markets(markets, _cfg(), S + 240), [now_market])
        self.assertEqual(seq_eligible_markets(markets, _cfg(), S + 241), [])
        self.assertEqual(seq_late_markets(markets, _cfg(), S + 241), [now_market])
        self.assertEqual(seq_late_markets(markets, _cfg(), S + 900), [])
        closed = _market(S, 4, accepting_orders=False)
        self.assertEqual(seq_eligible_markets([closed], _cfg(), S), [])
        self.assertEqual(
            seq_eligible_markets([closed], _cfg(require_accepting_orders=False), S), [closed]
        )
        self.assertEqual(seq_eligible_markets([_market(S, 5, neg_risk=True)], _cfg(), S), [])

    def test_busy_bag_is_live_and_uncashed_only(self):
        state = {"intents": {_cid(1): _prev_bag()}}
        self.assertEqual(seq_busy_bag(state, S - 10, ACTIVE)[0], _cid(1))
        self.assertIsNone(seq_busy_bag(state, S, ACTIVE), "window ended")
        self.assertIsNone(seq_busy_bag(state, S - 10, ACTIVE, exclude=_cid(1)))
        for extra in (
            {"sold_winner": True},
            {"status": "failed"},
            {"status": "completed"},
            {"dry_run": True},
        ):
            row = {"intents": {_cid(1): _prev_bag(**extra)}}
            self.assertIsNone(seq_busy_bag(row, S - 10, ACTIVE), extra)
        pending = {"intents": {_cid(1): _prev_bag(status="pending")}}
        self.assertIsNotNone(seq_busy_bag(pending, S - 10, ACTIVE))
        kept = {"intents": {_cid(1): _prev_bag(sold_loser=True, sold_leg="up")}}
        self.assertIsNotNone(seq_busy_bag(kept, S - 10, ACTIVE), "winner still held")

    def test_wait_log_throttle_and_single_skip(self):
        waits = SeqWaits(log_every_s=30.0)
        self.assertTrue(waits.note_wait("c", "prev_bag", 0.0))
        self.assertFalse(waits.note_wait("c", "prev_bag", 29.0))
        self.assertTrue(waits.note_wait("c", "cash", 31.0))
        self.assertFalse(waits.note_wait("c", "cash", 40.0))
        self.assertTrue(waits.note_wait("c", "cash", 61.0))
        self.assertEqual(waits.waited_s("c", 100.0), 100.0)
        first = waits.take_skip("c")
        self.assertEqual(first["reason"], "cash")
        self.assertIsNone(waits.take_skip("c"))
        self.assertEqual(waits.take_skip("never-waited"), {})


class SequentialMintCycleTests(unittest.TestCase):
    def test_sequential_off_keeps_the_14_minute_lookahead(self):
        nxt = _market(S + 840.0, 2)
        h = MintHarness(_cfg(mint_sequential=False), {"intents": {}}, [nxt], balance=1_000.0)
        self.assertEqual(h.tick(S), "submitted")
        self.assertEqual(h.named("mint_attempt")[0]["sequential"], False)
        self.assertIsNone(h.named("mint_attempt")[0]["seq_waited_s"])

    def test_sequential_does_not_mint_14_minutes_ahead(self):
        nxt = _market(S + 840.0, 2)
        h = MintHarness(_cfg(), {"intents": {}}, [nxt], balance=1_000.0)
        self.assertEqual(h.tick(S), "idle")
        self.assertEqual(h.submits, [])
        self.assertEqual(h.tick(S + 810.0), "submitted")

    def test_previous_bag_blocks_until_window_end_then_mints_at_open(self):
        state = {"intents": {_cid(1): _prev_bag()}}
        h = MintHarness(_cfg(), state, [_market(S, 2)], balance=1_000.0)
        self.assertEqual(h.tick(S - 30.0), "seq_wait_prev")
        self.assertEqual(h.tick(S - 20.0), "seq_wait_prev")
        waits = h.named("mint_seq_wait_prev")
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["prev_condition_id"], _cid(1))
        self.assertEqual(waits[0]["condition_id"], _cid(2))
        self.assertEqual(h.submits, [])
        self.assertEqual(h.tick(S), "submitted")
        self.assertEqual(state["intents"][_cid(2)]["status"], "pending")
        self.assertEqual(state["intents"][_cid(2)]["start_ts"], S)
        self.assertEqual(len(h.submits), 1)

    def test_cashed_winner_frees_the_next_mint_before_open(self):
        state = {"intents": {_cid(1): _prev_bag(sold_loser=True, sold_winner=True)}}
        h = MintHarness(_cfg(), state, [_market(S, 2)], balance=200.0)
        self.assertEqual(h.tick(S - 30.0), "submitted")

    def test_held_dump_counts_as_done(self):
        state = {"intents": {_cid(1): _prev_bag(sold_loser=True, sold_dump=True, sold_winner=True)}}
        h = MintHarness(_cfg(), state, [_market(S, 2)], balance=200.0)
        self.assertEqual(h.tick(S - 5.0), "submitted")

    def test_cash_short_waits_then_mints_when_redeem_lands(self):
        state = {"intents": {_cid(1): _prev_bag()}}
        h = MintHarness(_cfg(), state, [_market(S, 2)], balance=12.0)
        self.assertEqual(h.tick(S + 1.0), "seq_wait_cash")
        self.assertEqual(h.tick(S + 6.0), "seq_wait_cash")
        waits = h.named("mint_seq_wait_cash")
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["cash_reason"], "no_balance")
        self.assertEqual(waits[0]["need"], 200.0)
        self.assertEqual(h.tick(S + 31.0), "seq_wait_cash")
        self.assertEqual(len(h.named("mint_seq_wait_cash")), 2)
        self.assertEqual(h.named("mint_skip_balance"), [])
        h.chain.balance = 212.0
        self.assertEqual(h.tick(S + 150.0), "submitted")
        attempt = h.named("mint_attempt")[0]
        self.assertTrue(attempt["sequential"])
        self.assertEqual(attempt["seq_waited_s"], 149.0)

    def test_pending_reserve_is_a_wait_not_a_skip(self):
        state = {
            "intents": {
                _cid(1): _prev_bag(sold_winner=True),
                _cid(9): _prev_bag(status="pending", start_ts=S - 1800, end_ts=S - 900, shares=200.0),
            }
        }
        h = MintHarness(_cfg(), state, [_market(S, 2)], balance=250.0)
        self.assertEqual(h.tick(S), "seq_wait_cash")
        self.assertEqual(h.named("mint_seq_wait_cash")[0]["cash_reason"], "pending_reserve")

    def test_cash_never_back_skips_once_past_cutoff(self):
        state = {"intents": {_cid(1): _prev_bag()}}
        nxt = _market(S + 900.0, 3)
        h = MintHarness(_cfg(), state, [_market(S, 2), nxt], balance=0.0)
        self.assertEqual(h.tick(S + 10.0), "seq_wait_cash")
        self.assertEqual(h.tick(S + 241.0), "idle")
        skips = h.named("mint_seq_skip")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["condition_id"], _cid(2))
        self.assertEqual(skips[0]["last_wait"], "cash")
        self.assertEqual(skips[0]["waited_s"], 231.0)
        self.assertEqual(skips[0]["cutoff_s"], 240.0)
        h.tick(S + 300.0)
        self.assertEqual(len(h.named("mint_seq_skip")), 1)
        self.assertNotIn(_cid(2), state["intents"])
        h.chain.balance = 500.0
        self.assertEqual(h.tick(S + 870.0), "submitted")
        self.assertIn(_cid(3), state["intents"])

    def test_minted_window_never_logs_a_skip(self):
        state = {"intents": {}}
        h = MintHarness(_cfg(), state, [_market(S, 2)], balance=500.0)
        self.assertEqual(h.tick(S), "submitted")
        state["intents"][_cid(2)]["status"] = "confirmed"
        h.tick(S + 300.0)
        self.assertEqual(h.named("mint_seq_skip"), [])

    def test_own_bag_blocks_the_next_window_until_it_ends(self):
        state = {"intents": {}}
        cur, nxt = _market(S, 2), _market(S + 900.0, 3)
        h = MintHarness(_cfg(), state, [cur, nxt], balance=1_000.0)
        self.assertEqual(h.tick(S), "submitted")
        state["intents"][_cid(2)]["status"] = "confirmed"
        self.assertEqual(h.tick(S + 870.0), "seq_wait_prev")
        self.assertEqual(h.tick(S + 900.0), "submitted")
        self.assertEqual(len(h.submits), 2)

    def test_claim_gate_rechecks_the_busy_bag(self):
        state = {"intents": {_cid(1): _prev_bag()}}
        h = MintHarness(_cfg(), state, [], balance=1_000.0)
        claim = h.ns["_claim_mint_intent"]
        reason = claim(state, _cid(2), {"status": "submitting"}, _cfg(), S - 10.0, S)
        self.assertEqual(reason, "seq_wait_prev")
        self.assertIsNone(claim(state, _cid(2), {"status": "submitting"}, _cfg(), S, S))
        off = {"intents": {_cid(1): _prev_bag()}}
        reason = claim(off, _cid(2), {"status": "submitting"}, _cfg(mint_sequential=False), S - 10.0, S)
        self.assertIsNone(reason, "default adjacent-window lookahead unchanged")


class LateMintedBagSellTests(unittest.TestCase):
    """A bag minted at window open is sold exactly like one minted 14m ahead."""

    def test_loser_scrap_still_arms_and_fires(self):
        ns, events, fak_calls, clock, _book = _scrap_harness()
        start = clock["now"] - 300.0
        end = start + 900.0
        intent = _open_scrap_bag(end, start_ts=start, created_at=start + 20.0, submitted_at=start + 20.0)
        state = {"intents": {"cid-late": intent}}
        cfg = _scrap_cfg()
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertEqual(intent.get("sell_loser_armed_at"), clock["now"])
        clock["now"] += 5.0
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertEqual(fak_calls, [{"token_id": "up-tok", "size": 50.0, "price": 0.02, "dry_run": False}])
        self.assertTrue(intent.get("sold_loser"))
        names = [row["event"] for row in events]
        self.assertIn("sell_scrap_sweep", names)
        self.assertIn("sell_loser_done", names)

    def test_held_dump_still_fires(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        end = clock["now"] + 200.0
        intent = _held_after_scrap(end, start_ts=end - 900.0, created_at=end - 890.0)
        state = {"intents": {"cid-late": intent}}
        cfg = _dump_cfg()
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())
        clock["now"] += 2.0
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertEqual(len(fak_calls), 1)
        self.assertEqual(fak_calls[0]["token_id"], "up-tok")
        self.assertTrue(intent.get("sold_dump"))
        self.assertEqual(intent.get("sell_dump_leg"), "up")
        self.assertIn("sell_dump_done", [row["event"] for row in events])


if __name__ == "__main__":
    unittest.main()
