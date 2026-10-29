"""tick_lock_short: identical decisions, state and journal order, shorter lock hold."""
from __future__ import annotations

import ast
import copy
import json
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy.binance_trades import PriceGrid
from buy.fivemin import DEFAULTS, FiveMinIO, Runner, Strategy, settings
from buy.hotpath import TickEffects

ROOT = Path(__file__).resolve().parents[1]


def market(cid="cid", start=1000):
    return SimpleNamespace(condition_id=cid, slug=f"btc-up-or-down-5m-{start}",
                           start_ts=start, end_ts=start + 300, up_token=f"{cid}_up",
                           dn_token=f"{cid}_dn", active=True, closed=False, neg_risk=False)


def cfg(**extra):
    value = dict(DEFAULTS)
    value.update({"fivemin_enabled": True, "dry_run": False, "redeem_enabled": False})
    value.update(extra)
    return value


class Harness:
    """One Runner over a scripted tick stream, recording everything it emits."""

    def __init__(self, *, short: bool, start=1200.0, sniper=False):
        self.short, self.sniper = short, sniper
        self.clock = [start]
        self.logs = []
        self.saved = []
        self.buys = []
        self.sells = []
        self.quotes = {}
        self.fail_tick = None
        io = FiveMinIO(
            buy=lambda token, shares, limit, dry, **kw: self.buys.append(
                (token, shares, limit, dry)) or {"shares": shares, "cost": shares * limit,
                                                 "price": limit, "status": "matched"},
            sell=lambda row, pos, book, cfg, now: self.sells.append(
                (row["condition_id"], pos["shares"])) or {
                    "shares": pos["shares"], "proceeds": pos["shares"] * book["bid"],
                    "price": book["bid"], "status": "matched"},
            reserves=lambda cfg: 0,
            redeem=lambda row, now: None,
            save=lambda state: self.saved.append(json.loads(json.dumps(state))),
            log=lambda event, **kw: self.logs.append((event, kw)),
            alert=lambda text, **kw: self.logs.append(("alert", {"text": text, **kw})),
            durable=lambda: True,
            clock=lambda: self.clock[0],
        )
        self.strategy = Strategy(io, startup_ts=0)
        self.strategy.add_market(market(), now=start)
        row = self.strategy.state["markets"]["cid"]
        row["strike"] = 70_000.0
        row["offset"] = 0.0
        self.strategy.sample_cash(start, 1_000.0)
        self.grid = PriceGrid()
        # 120s of 10s-spaced prints so twap60/offset/proxy resolve, as live.
        for step in range(130, -1, -10):
            ms = (start - step) * 1000.0
            self.grid.add(ms, 70_000.0, source="primary", recv_ms=ms, recv_wall_ms=ms)
        self.books = SimpleNamespace(
            set_tokens=lambda tokens: None, set_stale_s=lambda _s: None,
            set_min_size=lambda _n: None, usable=lambda tok: self._usable(tok),
            reason=lambda tok: None, source=lambda tok: "ws", poll=lambda *a: None,
        )
        self.binance_stub = SimpleNamespace(start=lambda: None, stop=lambda: None, _on_tick=None)
        self.runner = Runner(
            self.strategy,
            lambda: cfg(tick_lock_short=short, sniper_5m_enabled=sniper, fivemin_gate_k=0.0),
            SimpleNamespace(discover=lambda slugs: []),
            SimpleNamespace(pUSD_balance=lambda *a: 1_000.0),
            "funder", self.grid, self.binance_stub,
            SimpleNamespace(start=lambda: None, stop=lambda: None, drain=lambda: [],
                            reconnect=lambda reason: None),
            self.books, clock=lambda: self.clock[0],
        )
        self.runner.current = self.strategy.state["markets"]["cid"]

    def reset_market(self, start: float, *, ttm_s: float = 120.0):
        """Drop the row and re-add a market already ``ttm_s`` from its close."""
        self.strategy.state["markets"].clear()
        self.strategy._active.clear()
        self.strategy.runs.clear()
        self.strategy.add_market(market(start=int(start) - (300 - int(ttm_s))), now=start)
        row = self.strategy.state["markets"]["cid"]
        row["strike"] = 70_000.0
        row["offset"] = 0.0
        self.strategy.sample_cash(start, 1_000.0)
        self.runner.current = row
        return row

    def _usable(self, token):
        side = "up" if token.endswith("_up") else "down"
        quote = self.quotes.get(side)
        if quote is None:
            return None
        return SimpleNamespace(bid=quote["bid"], ask=quote["ask"], bids=quote["bids"],
                               asks=quote["asks"], age=0.05)

    def step(self, now, quotes, price=70_000.0):
        """One tick with a fresh primary print, so the BTC staleness gate stays open."""
        self.clock[0] = now
        ms = now * 1000.0
        self.grid.add(ms, price, source="primary", recv_ms=ms, recv_wall_ms=ms)
        self.quotes = quotes
        self.runner.current = self.strategy.state["markets"]["cid"]
        if self.fail_tick is not None:
            self.strategy.tick, saved_tick = self.fail_tick, self.strategy.tick
            try:
                self.runner.tick()
            finally:
                self.strategy.tick = saved_tick
        else:
            self.runner.tick()

    def feed_tick(self):
        """One event-driven sniper wake-up, as the Binance feed thread does."""
        self.runner.on_binance_tick(self.clock[0] * 1000.0, 70_000.0)

    def snapshot(self):
        return {
            "logs": [(event, json.loads(json.dumps(fields, default=str))) for event, fields in self.logs],
            "saved": copy.deepcopy(self.saved),
            "buys": list(self.buys),
            "sells": list(self.sells),
            "state": json.loads(json.dumps(self.strategy.state, default=str)),
            "runs": json.loads(json.dumps(self.strategy.runs, default=str)),
        }


def _normalise(snapshot):
    """Drop the fields that legitimately differ (timestamps, wall-clock ages)."""
    # Wall-clock stamps and measured latency legitimately differ between runs.
    drop = {
        "ts", "age_ms", "latency_ms", "decision_ts", "attempted_at", "filled_at",
        "decision_ts", "sign_done_ts", "send_start_ts", "resp_first_byte_ts",
        "resp_complete_ts", "trigger_exchange_ts", "trigger_recv_ts", "server_body_ts",
        "recv_to_decision_ms", "trigger_exchange_to_decision_ms", "decision_to_send_ms",
        "send_to_complete_ms", "trigger_exchange_to_complete_ms", "recv_to_complete_ms",
        "feed_recv", "transport_ms", "fire_event_recv_ts", "first_seen_ts",
        "first_seen_to_decision_ms", "fire_event_to_decision_ms", "ttfb_ms", "body_ms",
    }
    logs = []
    for event, fields in snapshot["logs"]:
        if event == "alert":
            logs.append((event, {"text": fields["text"], "kind": fields.get("kind")}))
            continue
        logs.append((event, {k: v for k, v in fields.items() if k not in drop}))
    return logs


def _book(bid, ask, size=40.0):
    return {"bid": bid, "ask": ask, "bids": [[bid, size]], "asks": [[ask, size]]}


# Entry on a cheap loser, a held-leg dump, then settlement. ``price`` moves the
# strike basis so the dumps are driven by the book, not by the BTC gate.
BOOK_SCRIPT = [
    (1201.0, {"up": _book(0.50, 0.52), "down": _book(0.48, 0.50)}, 70_000.0),
    (1204.0, {"up": _book(0.05, 0.06), "down": _book(0.93, 0.95)}, 70_000.0),
    (1207.5, {"up": _book(0.04, 0.05), "down": _book(0.94, 0.96)}, 70_000.0),
    (1210.0, {"up": _book(0.04, 0.05), "down": _book(0.94, 0.96)}, 70_000.0),
    (1213.0, {"up": _book(0.75, 0.78), "down": _book(0.18, 0.20)}, 70_000.0),
    (1217.0, {"up": _book(0.75, 0.78), "down": _book(0.18, 0.20)}, 70_000.0),
    (1221.0, {"up": _book(0.75, 0.78), "down": _book(0.18, 0.20)}, 70_000.0),
    (1299.5, {"up": _book(0.99, 1.0, 10.0), "down": _book(0.01, 0.02, 10.0)}, 70_000.0),
]


def _replay(short: bool, script=BOOK_SCRIPT, sniper=False) -> dict:
    harness = Harness(short=short, sniper=sniper)
    for row in script:
        harness.step(*row)
    return harness.snapshot()


class ReplayEquivalenceTest(unittest.TestCase):
    """Same scripted ticks, both modes: identical decisions, state and journal order."""

    def test_decisions_state_and_journal_are_identical(self):
        long_run = _replay(False)
        short_run = _replay(True)
        self.assertEqual(_normalise(long_run), _normalise(short_run))
        self.assertEqual(long_run["state"], short_run["state"])
        self.assertEqual(long_run["runs"], short_run["runs"])
        self.assertEqual(long_run["buys"], short_run["buys"])
        self.assertEqual(long_run["sells"], short_run["sells"])
        # The journal must not be empty, or the comparison proves nothing.
        self.assertGreater(len(long_run["logs"]), 5)

    def test_buy_and_sell_decisions_both_happen(self):
        run = _replay(True)
        self.assertTrue(run["buys"], "scripted book should fire at least one entry")
        self.assertIn("cid", [cid for cid, _ in run["sells"]])

    def test_saves_keep_state_at_mutation_time(self):
        long_run = _replay(False)
        short_run = _replay(True)
        self.assertTrue(long_run["saved"])
        self.assertEqual(len(long_run["saved"]), len(short_run["saved"]))
        self.assertEqual(long_run["saved"], short_run["saved"])

    def test_a_raising_tick_flushes_what_it_logged(self):
        long_h = Harness(short=False)
        short_h = Harness(short=True)
        calls = {"n": 0}

        def boom(*args, **kwargs):
            raise RuntimeError("tick failed")

        for harness in (long_h, short_h):
            harness.step(1201.0, {"up": {"bid": 0.5, "ask": 0.52, "bids": [], "asks": []},
                                  "down": {"bid": 0.48, "ask": 0.5, "bids": [], "asks": []}})
            harness.fail_tick = boom
            with self.assertRaises(RuntimeError):
                harness.step(1202.0, {"up": {"bid": 0.5, "ask": 0.52, "bids": [], "asks": []},
                                      "down": {"bid": 0.48, "ask": 0.5, "bids": [], "asks": []}})
        self.assertEqual(_normalise(long_h.snapshot()), _normalise(short_h.snapshot()))
        self.assertEqual(calls["n"], 0)


def _sniper_replay(short: bool) -> dict:
    """The event-driven sniper path: feed wake-up -> check -> buy, one trigger."""
    harness = Harness(short=short, sniper=True)
    # A $30 jump inside the 500ms window, $30 above the strike.
    harness.grid.add(1_199_600.0, 70_000.0, source="primary",
                     recv_ms=1_199_600.0, recv_wall_ms=1_199_600.0)
    harness.grid.add(1_200_000.0, 70_030.0, source="primary",
                     recv_ms=1_200_000.0, recv_wall_ms=1_200_000.0)
    harness.clock[0] = 1200.0
    harness.quotes = {"up": _book(0.92, 0.94, 20.0), "down": _book(0.06, 0.08, 20.0)}
    harness.runner.current = harness.strategy.state["markets"]["cid"]
    harness.feed_tick()
    return harness.snapshot()


class SniperPathEquivalenceTest(unittest.TestCase):
    """tick_lock_short must not change the sniper's decision or its journal."""

    def test_feed_triggered_buy_and_latency_line_match(self):
        long_run = _sniper_replay(False)
        short_run = _sniper_replay(True)
        self.assertEqual(len(long_run["buys"]), 1)
        self.assertEqual(long_run["buys"], short_run["buys"])
        self.assertEqual([e for e, _ in long_run["logs"]],
                         [e for e, _ in short_run["logs"]])
        self.assertIn("sniper_order_latency", [e for e, _ in long_run["logs"]])
        self.assertEqual(_normalise(long_run), _normalise(short_run))
        self.assertEqual(long_run["state"], short_run["state"])

    def test_state_transition_marks_the_sniper_attempt_once(self):
        run = _sniper_replay(True)
        sniper = run["state"]["markets"]["cid"]["sniper_5m"]
        self.assertTrue(sniper["attempted"])
        self.assertEqual(sniper["side"], "up")
        self.assertAlmostEqual(sniper["jump_usd"], 30.0, places=6)
        self.assertEqual(sniper["position"]["side"], "up")


class LockHoldTest(unittest.TestCase):
    """The strategy tick logs while the lock is held; the flush must not."""

    def _harness(self, short, log):
        harness = Harness(short=short)
        harness.strategy.io.log = log
        harness.strategy.io.alert = lambda text, **kw: None
        original = harness.strategy.tick

        def logging_tick(cfg, now, grid, quotes):
            harness.strategy.io.log("tick_probe", n=1)
            return original(cfg, now, grid, quotes)

        harness.strategy.tick = logging_tick
        return harness

    def _compete(self, harness, delay=0.0):
        waited = []

        def run():
            time.sleep(delay)
            began = time.perf_counter()
            acquired = harness.runner._tick_lock.acquire(timeout=2.0)
            waited.append((acquired, time.perf_counter() - began))
            if acquired:
                harness.runner._tick_lock.release()

        return waited, run

    def test_short_mode_flushes_after_releasing_the_lock(self):
        entered = threading.Event()
        release = threading.Event()
        seen = []

        def gated_log(event, **fields):
            seen.append(event)
            if event == "tick_probe":
                entered.set()
                release.wait(2.0)

        harness = self._harness(True, gated_log)
        quotes = {"up": _book(0.50, 0.52), "down": _book(0.48, 0.50)}
        worker = threading.Thread(target=harness.step, args=(1201.0, quotes), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(2.0), "tick must reach the journal")
        waited, compete = self._compete(harness)
        racer = threading.Thread(target=compete, daemon=True)
        racer.start()
        racer.join(timeout=2.0)
        release.set()
        worker.join(timeout=5.0)
        self.assertTrue(waited and waited[0][0])
        self.assertLess(waited[0][1], 0.05,
                        "tick_lock_short must not hold _tick_lock during the journal write")
        self.assertIn("tick_probe", seen)

    def test_short_mode_flushes_even_when_the_tick_raises(self):
        seen = []
        harness = self._harness(True, lambda event, **fields: seen.append(event))

        def boom(cfg, now, grid, quotes):
            harness.strategy.io.log("before_failure", n=1)
            raise RuntimeError("tick failed")

        harness.strategy.tick = boom
        harness.fail_tick = boom
        with self.assertRaises(RuntimeError):
            harness.step(1201.0, {"up": _book(0.5, 0.52), "down": _book(0.48, 0.5)})
        self.assertIn("before_failure", seen)
        self.assertTrue(harness.runner._tick_lock.acquire(timeout=0.1))
        harness.runner._tick_lock.release()

    def test_long_mode_holds_the_lock_through_the_journal_write(self):
        entered = threading.Event()
        release = threading.Event()

        def gated_log(event, **fields):
            if event == "tick_probe":
                entered.set()
                release.wait(2.0)

        harness = self._harness(False, gated_log)
        quotes = {"up": _book(0.50, 0.52), "down": _book(0.48, 0.50)}
        worker = threading.Thread(target=harness.step, args=(1201.0, quotes), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(2.0))
        waited, compete = self._compete(harness)
        racer = threading.Thread(target=compete, daemon=True)
        racer.start()
        racer.join(timeout=0.3)
        self.assertFalse(waited, "the old path logs while holding the lock")
        release.set()
        worker.join(timeout=5.0)
        racer.join(timeout=3.0)
        self.assertTrue(waited and waited[0][0])

    def test_default_off_runs_the_old_inline_logging(self):
        seen = []
        harness = Harness(short=False)
        harness.strategy.io.log = lambda event, **fields: seen.append(event)
        harness.strategy.io.alert = lambda text, **kw: None
        captured = []
        original = harness.strategy.tick

        def probe(cfg, now, grid, quotes):
            captured.append(getattr(cfg, "get", lambda *_a: False)("tick_lock_short", False))
            return original(cfg, now, grid, quotes)

        harness.strategy.tick = probe
        harness.step(1201.0, {"up": _book(0.5, 0.52), "down": _book(0.48, 0.5)})
        self.assertEqual(captured, [False])


class TickEffectsTest(unittest.TestCase):
    def _io(self):
        seen = []
        io = SimpleNamespace(log=lambda event, **kw: seen.append(("log", event, kw)),
                             alert=lambda text, **kw: seen.append(("alert", text, kw)))
        return io, seen

    def test_buffers_then_replays_in_call_order(self):
        io, seen = self._io()
        effects = TickEffects(io)
        effects.log("a", x=1)
        effects.alert("b", kind="k")
        effects.log("c", x=2)
        self.assertEqual(seen, [])
        self.assertEqual(len(effects), 3)
        self.assertEqual(effects.flush(), 3)
        self.assertEqual([s[0] for s in seen], ["log", "alert", "log"])
        self.assertEqual([s[1] for s in seen], ["a", "b", "c"])
        self.assertEqual(len(effects), 0)
        self.assertEqual(effects.flush(), 0)

    def test_other_threads_pass_straight_through(self):
        io, seen = self._io()
        effects = TickEffects(io)
        worker = threading.Thread(target=lambda: effects.log("from-worker", x=1))
        worker.start()
        worker.join(2.0)
        self.assertEqual(seen, [("log", "from-worker", {"x": 1})])
        self.assertEqual(len(effects), 0)

    def test_replay_failure_does_not_stop_later_items(self):
        seen = []

        def flaky(event, **kw):
            seen.append(event)
            if event == "bad":
                raise RuntimeError("journal down")

        io = SimpleNamespace(log=flaky, alert=lambda *a, **kw: None)
        effects = TickEffects(io)
        effects.log("good")
        effects.log("bad")
        effects.log("after")
        self.assertEqual(effects.flush(), 3)
        self.assertEqual(seen, ["good", "bad", "after"])

    def test_swapping_io_log_does_not_recurse(self):
        io, seen = self._io()
        effects = TickEffects(io)
        io.log = effects.log
        effects.log("x", n=1)
        self.assertEqual(seen, [])
        effects.flush()
        self.assertEqual(seen, [("log", "x", {"n": 1})])

    def test_log_fields_builds_the_payload_at_flush_time(self):
        io, seen = self._io()
        effects = TickEffects(io)
        built = []
        effects.log_fields("deferred", lambda: built.append(1) or {"v": 7})
        self.assertEqual(built, [])
        effects.flush()
        self.assertEqual(seen, [("log", "deferred", {"v": 7})])
        self.assertEqual(built, [1])


class FlagWiringTest(unittest.TestCase):
    def test_default_off_in_defaults_and_example(self):
        example = json.loads((ROOT / "strategy_mint.example.json").read_text())
        defaults = DEFAULTS
        for key in ("tick_lock_short",):
            self.assertIn(key, defaults)
            self.assertIs(defaults[key], False)
        tree = ast.parse((ROOT / "mintbot.py").read_text())
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "DEFAULTS":
                names = ast.literal_eval(node.value)
        self.assertIn("tick_lock_short", names)
        self.assertIs(names["tick_lock_short"], False)
        self.assertIn("tick_lock_short", example)
        self.assertIs(example["tick_lock_short"], False)

    def test_settings_accepts_the_flag(self):
        self.assertIs(settings(cfg(tick_lock_short=True))["tick_lock_short"], True)


if __name__ == "__main__":
    unittest.main()
