"""Held dump and kept sell post together when sell_dump_parallel_kept is on."""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path

from buy.mint_sell import DEFAULT_SELL_KNOBS
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_only_ops import MINT_EXAMPLE, _assign


def _tick(ns, cfg, intent, cid="cid-kept"):
    state = {"intents": {cid: intent}}
    ns["remember_persisted_state"](state)
    ns["_manage_sells_locked"](cfg, state, object())
    return intent


def _ready(clock, keep=25.0):
    return _held_after_scrap(
        clock["now"] + 200.0,
        sell_dump_armed_at=clock["now"] - 2.0,
        sell_scrap_keep=keep,
        sell_scrap_target=50.0 - keep,
        sell_filled=50.0 - keep,
    )


def _cfg(**extra):
    cfg = _dump_cfg(sell_dump_also_kept=True, sell_dump_parallel_kept=True)
    cfg.update(extra)
    return cfg


class DumpParallelKeptTests(unittest.TestCase):
    def test_both_legs_are_in_flight_together(self):
        ns, _events, fak_calls, clock = _dump_harness(_fill_fak)
        lock = threading.Lock()
        inflight = {"n": 0, "max": 0}
        entered = {"up-tok": threading.Event(), "dn-tok": threading.Event()}

        def fak(token, size, price, dry, capture=None, decision_at=None):
            fak_calls.append(
                {"token_id": token, "size": float(size), "price": float(price)}
            )
            with lock:
                inflight["n"] += 1
                inflight["max"] = max(inflight["max"], inflight["n"])
            entered[token].set()
            self.assertTrue(entered["up-tok"].wait(2.0))
            self.assertTrue(entered["dn-tok"].wait(2.0))
            with lock:
                inflight["n"] -= 1
            return float(size), "matched"

        ns["_fak_sell"] = fak
        intent = _ready(clock)
        _tick(ns, _cfg(), intent)
        self.assertGreaterEqual(inflight["max"], 2)
        self.assertTrue(intent.get("sold_dump"))
        self.assertEqual(intent.get("sell_dump_kept_outcome"), "filled")
        self.assertCountEqual(
            [row["token_id"] for row in fak_calls],
            ["up-tok", "dn-tok"],
        )

    def test_kept_still_sells_when_the_held_leg_misses(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        entered = {"up-tok": threading.Event(), "dn-tok": threading.Event()}

        def fak(token, size, price, dry, capture=None, decision_at=None):
            fak_calls.append({"token_id": token, "size": float(size), "price": float(price)})
            entered[token].set()
            self.assertTrue(entered["up-tok"].wait(2.0))
            self.assertTrue(entered["dn-tok"].wait(2.0))
            if token == "up-tok":
                return 0.0, "error:timeout"
            return float(size), "matched"

        ns["_fak_sell"] = fak
        intent = _ready(clock)
        _tick(ns, _cfg(), intent)
        self.assertFalse(intent.get("sold_dump"))
        self.assertEqual(intent.get("sell_dump_kept_outcome"), "filled")
        self.assertAlmostEqual(intent.get("sell_dump_kept_filled"), 25.0)
        self.assertTrue(any(row["event"] == "sell_dump_kept" for row in events))
        self.assertFalse(any(row["event"] == "sell_dump_done" for row in events))
        calls = len(fak_calls)
        clock["now"] += 5.0
        _tick(ns, _cfg(), intent)
        self.assertEqual(
            [row["token_id"] for row in fak_calls[calls:]],
            ["up-tok"],
        )

    def test_reclaim_arms_only_after_both_legs_return(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        entered = {"up-tok": threading.Event(), "dn-tok": threading.Event()}
        saw_arm_during_post = {"n": 0}

        def fak(token, size, price, dry, capture=None, decision_at=None):
            fak_calls.append({"token_id": token})
            if any(row["event"] == "reclaim_arm" for row in events):
                saw_arm_during_post["n"] += 1
            entered[token].set()
            self.assertTrue(entered["up-tok"].wait(2.0))
            self.assertTrue(entered["dn-tok"].wait(2.0))
            if any(row["event"] == "reclaim_arm" for row in events):
                saw_arm_during_post["n"] += 1
            return float(size), "matched"

        ns["_fak_sell"] = fak
        intent = _ready(clock)
        _tick(ns, _cfg(reclaim_enabled=True), intent)
        self.assertEqual(saw_arm_during_post["n"], 0)
        self.assertTrue(intent.get("sold_dump"))
        self.assertTrue(intent.get("sell_dump_kept_done"))
        self.assertTrue(intent.get("reclaim_armed"))
        self.assertTrue(any(row["event"] == "reclaim_arm" for row in events))

    def test_flag_off_keeps_the_kept_sell_behind_the_held_fill(self):
        ns, _events, fak_calls, clock = _dump_harness(_fill_fak)
        phases: list = []
        lock = threading.Lock()
        inflight = {"n": 0, "max": 0}

        def fak(token, size, price, dry, capture=None, decision_at=None):
            with lock:
                inflight["n"] += 1
                inflight["max"] = max(inflight["max"], inflight["n"])
            phases.append(("enter", token))
            time.sleep(0.05)
            phases.append(("exit", token))
            fak_calls.append({"token_id": token, "size": float(size)})
            with lock:
                inflight["n"] -= 1
            return float(size), "matched"

        ns["_fak_sell"] = fak
        intent = _ready(clock)
        _tick(ns, _cfg(sell_dump_parallel_kept=False), intent)
        self.assertEqual(inflight["max"], 1)
        self.assertEqual(
            phases,
            [
                ("enter", "up-tok"),
                ("exit", "up-tok"),
                ("enter", "dn-tok"),
                ("exit", "dn-tok"),
            ],
        )
        self.assertTrue(intent.get("sold_dump"))
        self.assertEqual(intent.get("sell_dump_kept_outcome"), "filled")

    def test_default_is_on_in_code_and_example(self):
        defaults = _assign("DEFAULTS")
        example = json.loads(Path(MINT_EXAMPLE).read_text())
        self.assertIs(defaults["sell_dump_parallel_kept"], True)
        self.assertIs(DEFAULT_SELL_KNOBS["sell_dump_parallel_kept"], True)
        self.assertIs(example["sell_dump_parallel_kept"], True)
        src = Path(__file__).resolve().parents[1].joinpath("mintbot.py").read_text()
        self.assertIn('cfg.get("sell_dump_parallel_kept", True)', src)
        self.assertIn("kept_thread.join()", src)
