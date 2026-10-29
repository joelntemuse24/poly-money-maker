#!/usr/bin/env python3
"""End-to-end trigger-to-send latency: in-process feeds vs a separate feed process.

Both modes run the *same* sniper code (``Strategy._sniper_check`` ->
``_sniper_execute`` -> ``io.buy``). Only where the jump is detected differs:

``in_process``
    A thread appends ticks to the local ``PriceGrid`` and calls
    ``Runner.on_binance_tick`` - i.e. ``BinanceTrades``'s feed thread.

``feed_process``
    A ``fork``ed child owns the grid and the detector, publishes a trigger into
    shared memory and wakes the parent over ``eventfd``. ``Runner.on_feed_trigger``
    picks it up on its own thread.

``io.buy`` is stubbed: it records ``perf_counter_ns`` and returns immediately, so
the number is *trigger -> the instant our code would start the POST*. There is no
network here; the POST is identical in both modes.

``--load`` adds a CPU-busy main process and a journal sink with live
``log_event`` cost, which is the case the split is meant to help: the detector no
longer waits behind the main process's GIL.

Caveat, stated plainly: the ``feed_process`` row includes the benchmark's own
shared-ring write and trigger write, not a websocket ``recv()``. It measures the
IPC path, not the network.

Usage:
    python tools/bench_feed_process.py --triggers 200
    python tools/bench_feed_process.py --triggers 200 --load --json /tmp/feed.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import threading
import time
import types
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from buy.feed_process import (  # noqa: E402
    FeedProcessClient, H_TICKS, TickRingWriter, write_trigger,
)
from buy.fivemin import Runner  # noqa: E402
from test_tick_lock_short import Harness, _book  # noqa: E402

JUMP_USD = 25.0
WINDOW_MS = 500
BASE_S = 1_700_000_000.0  # epoch seconds, matching the strategy clock
ITER_S = 400.0
DEFAULT_TRIGGERS = 150
QUOTES = {"up": _book(0.92, 0.94, 20.0), "down": _book(0.06, 0.08, 20.0)}


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _cfg(**extra) -> dict:
    value = {
        "fivemin_enabled": True, "dry_run": False, "redeem_enabled": False,
        "sniper_5m_enabled": True, "sniper_event_driven": True,
        "sniper_jump_usd": JUMP_USD, "sniper_window_ms": WINDOW_MS,
        "sniper_min_gap_usd": 20.0, "sniper_max_ask": 0.97, "sniper_min_ask": 0.50,
        "sniper_ttm_min_s": 15, "sniper_ttm_max_s": 240, "sniper_shares": 10,
        "sniper_use_offset": True, "sniper_extra_ws_enabled": False,
        "fivemin_gate_k": 0.0, "feed_process_status_s": 60.0,
    }
    value.update(extra)
    return value


class _Busy(threading.Thread):
    """Keeps the GIL contended, like the live sell/mint loops do."""

    def __init__(self):
        super().__init__(name="bench-load", daemon=True)
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            for _ in range(400):
                pass


class _Journal:
    """A journal sink with live ``log_event`` cost (plus a little extra work)."""

    def __init__(self, ns: int = 0):
        self.ns = int(ns)
        self.lines = 0

    def log(self, event, **fields) -> None:
        if self.ns:
            deadline = time.perf_counter_ns() + self.ns
            while time.perf_counter_ns() < deadline:
                pass
        self.lines += 1

    def alert(self, text, **fields) -> None:
        self.lines += 1


def _harness(cfg: dict, journal, load: bool):
    harness = Harness(short=True, sniper=True)
    harness.runner = Runner(
        harness.strategy, lambda: cfg,
        types.SimpleNamespace(discover=lambda slugs: []),
        types.SimpleNamespace(pUSD_balance=lambda *a: 1_000.0),
        "funder", harness.grid, harness.binance_stub,
        types.SimpleNamespace(start=lambda: None, stop=lambda: None, drain=lambda: [],
                              reconnect=lambda reason: None),
        harness.books, clock=lambda: harness.clock[0],
    )
    harness.runner.current = harness.strategy.state["markets"]["cid"]
    harness.strategy.io.log = journal.log
    harness.strategy.io.alert = journal.alert
    busy = _Busy()
    if load:
        busy.start()
    return harness, busy


def measure_in_process(triggers: int = DEFAULT_TRIGGERS, *, load: bool = False) -> dict:
    stamps: list[float] = []
    journal = _Journal(150_000 if load else 0)

    def buy(token, shares, limit, dry, **kw):
        stamps.append(time.perf_counter_ns())
        return {"shares": shares, "cost": shares * limit, "price": limit, "status": "matched"}

    harness, busy = _harness(_cfg(), journal, load)
    harness.strategy.io.buy = buy
    samples: list[float] = []
    try:
        for n in range(triggers):
            now = BASE_S + n * ITER_S
            row = harness.reset_market(now, ttm_s=200.0)
            harness.quotes = QUOTES
            harness.runner.current = row
            grid = harness.grid
            pre_ms, jump_ms = (now - 0.4) * 1000.0, now * 1000.0
            spot = 70_000.0 + JUMP_USD + 10.0
            grid.add(pre_ms, 70_000.0, source="primary", recv_ms=pre_ms,
                     recv_wall_ms=pre_ms)
            harness.clock[0] = now
            published = time.perf_counter_ns()
            grid.add(jump_ms, spot, source="primary", recv_ms=jump_ms, recv_wall_ms=jump_ms)
            before = len(stamps)
            harness.runner.on_binance_tick(jump_ms, spot)
            samples.append((stamps[-1] - published) / 1000.0 if len(stamps) > before else math.nan)
    finally:
        busy.stop.set()
        if busy.is_alive():
            busy.join(timeout=2.0)
    return _summarise("in_process", samples, triggers)


def measure_feed_process(triggers: int = DEFAULT_TRIGGERS, *, load: bool = False) -> dict:
    stamps: list[float] = []
    journal = _Journal(150_000 if load else 0)

    def buy(token, shares, limit, dry, **kw):
        stamps.append(time.perf_counter_ns())
        return {"shares": shares, "cost": shares * limit, "price": limit, "status": "matched"}

    harness, busy = _harness(_cfg(feed_process=True), journal, load)
    harness.strategy.io.buy = buy
    samples: list[float] = []
    client_cfg = _cfg(feed_process=True, _feed_process_fake_feed=True)
    client = FeedProcessClient(client_cfg, on_trigger=harness.runner.on_feed_trigger,
                               log=lambda *a, **k: None)
    try:
        if not client.start():
            raise RuntimeError("feed process did not start")
        if client.block is None:
            raise RuntimeError("no shared block")
        # Same swap ``Runner._start_feed_process`` performs.
        harness.runner.local_grid = harness.runner.grid
        harness.runner.grid = client.grid
        writer = TickRingWriter(client.block, client.layout)
        ring_base = int(client.block.i64[client.layout.header_index(H_TICKS)])
        for n in range(triggers):
            now = BASE_S + n * ITER_S
            row = harness.reset_market(now, ttm_s=200.0)
            harness.quotes = QUOTES
            harness.runner.current = row
            pre_ms, jump_ms = (now - 0.4) * 1000.0, now * 1000.0
            spot = 70_000.0 + JUMP_USD + 10.0
            writer.append("primary", pre_ms, 70_000.0)
            harness.clock[0] = now
            before = len(stamps)
            # Publish the jump print, the trigger and the wakeup: this is where
            # the child would be, measured from its own "tick accepted".
            published = time.perf_counter_ns()
            writer.append("primary", jump_ms, spot)
            write_trigger(client.block, client.layout, exchange_ms=jump_ms,
                          recv_wall_ms=jump_ms, spot=spot, raw_spot=spot,
                          low=70_000.0, high=spot, up_jump=JUMP_USD + 10.0,
                          down_jump=0.0, source="primary")
            try:
                os.write(client.wake_fd, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            except Exception:
                pass
            deadline = time.perf_counter() + 2.0
            while len(stamps) == before and time.perf_counter() < deadline:
                time.sleep(0.0001)
            samples.append((stamps[-1] - published) / 1000.0 if len(stamps) > before else math.nan)
        del ring_base
    finally:
        client.stop()
        busy.stop.set()
        if busy.is_alive():
            busy.join(timeout=2.0)
    return _summarise("feed_process", samples, triggers)


def _summarise(mode: str, samples: list[float], triggers: int) -> dict:
    good = [s for s in samples if s == s]  # drop NaN (no order fired)
    return {
        "mode": mode,
        "triggers": triggers,
        "fired": len(good),
        "p50_us": round(statistics.median(good), 1) if good else None,
        "p90_us": round(_percentile(good, 0.90), 1) if good else None,
        "p99_us": round(_percentile(good, 0.99), 1) if good else None,
        "max_us": round(max(good), 1) if good else None,
        "mean_us": round(statistics.fmean(good), 1) if good else None,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--triggers", type=int, default=DEFAULT_TRIGGERS)
    parser.add_argument("--load", action="store_true", help="keep the main process CPU-busy")
    parser.add_argument("--json", help="write the results here as JSON")
    parser.add_argument("--switch", type=float, default=None,
                        help="also set sys.setswitchinterval before measuring")
    args = parser.parse_args(argv)

    if args.switch:
        sys.setswitchinterval(float(args.switch))
    rows = [measure_in_process(args.triggers, load=args.load),
            measure_feed_process(args.triggers, load=args.load)]
    rows[0]["switch_s"] = sys.getswitchinterval()
    rows[1]["switch_s"] = sys.getswitchinterval()
    print(f"{'mode':>14} {'fired':>6} {'p50 us':>9} {'p90 us':>9} {'p99 us':>9} {'max us':>9}")
    for row in rows:
        print(f"{row['mode']:>14} {row['fired']:>6} {row['p50_us']:>9} {row['p90_us']:>9} "
              f"{row['p99_us']:>9} {row['max_us']:>9}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
