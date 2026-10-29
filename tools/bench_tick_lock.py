#!/usr/bin/env python3
"""Measure how long ``Runner._tick_lock`` is held, with and without ``tick_lock_short``.

Two scenarios, each run in both modes:

``cycle``
    Repeated book cycles that each fire a main entry and then a held-leg dump, so
    every iteration writes ``fivemin_eval``/``fivemin_signal``/``fivemin_fill``/
    ``fivemin_dump`` plus alerts: the routine journal load.

``sniper``
    The event-driven sniper path (feed wake-up -> jump check -> FAK buy), which
    emits ``fivemin_signal``, ``fivemin_sniper_fill``, ``sniper_order_latency`` and
    an alert - the lines whose JSON encoding used to sit between the decision and
    the POST.

Two numbers are reported per row:

``hold``
    How long ``_tick_lock`` was held, timed by wrapping the RLock itself.

``sink``
    Total time spent inside the journal sink. The sink mimics
    ``mintbot.log_event`` (``json.dumps(payload, default=str)``), so this is the
    serialization cost that ``tick_lock_short`` moves out of the lock. It is a
    *subset* of ``hold`` in the default mode and disjoint from it with the flag on,
    which is the whole point of the change.

``trigger``
    For ``sniper`` only: the lock hold of the one tick that fires the order, i.e.
    the interval the feed thread's trigger must wait out before ``_fak_buy``.

Deliberately not measured: how long another thread has to wait for the lock. A
Python thread cannot be preempted at a lock handoff faster than the GIL switch
interval, so a competing-thread probe reports schedulating noise (it varies by
an order of magnitude between runs) rather than the hold time. ``hold`` is the
honest, repeatable number.

Usage:
    python tools/bench_tick_lock.py --ticks 300 --repeats 5
    python tools/bench_tick_lock.py --ticks 300 --json /tmp/tick_lock.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from test_tick_lock_short import Harness, _book  # noqa: E402

TICK_S = 0.02  # Runner poll cadence, so the script mimics the live loop.
DEFAULT_TICKS = 300
DEFAULT_REPEATS = 5
QUOTES = {"up": _book(0.50, 0.52), "down": _book(0.48, 0.50)}


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


class _TimedLock:
    """Transparent RLock wrapper that records how long it is held."""

    def __init__(self, inner):
        self._inner = inner
        self.holds: list[float] = []
        self._local = threading.local()

    def __enter__(self):
        self._inner.acquire()
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        stack.append(time.perf_counter_ns())
        return self

    def __exit__(self, *exc):
        stack = getattr(self._local, "stack", [])
        if stack:
            started = stack.pop()
            self.holds.append((time.perf_counter_ns() - started) / 1000.0)
        self._inner.release()
        return False

    def acquire(self, *a, **kw):
        return self._inner.acquire(*a, **kw)

    def release(self):
        return self._inner.release()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _Sink:
    """Journal sink with the cost profile of ``mintbot.log_event``."""

    def __init__(self):
        self.lines: list[str] = []
        self.ns = 0

    def log(self, event, **fields) -> None:
        began = time.perf_counter_ns()
        self.lines.append(json.dumps({"ts": time.time(), "event": event, **fields}, default=str))
        self.ns += time.perf_counter_ns() - began

    def alert(self, text, **fields) -> None:
        began = time.perf_counter_ns()
        self.lines.append(f"alert:{text}")
        self.ns += time.perf_counter_ns() - began


CYCLE = [
    {"up": _book(0.50, 0.52), "down": _book(0.48, 0.50)},
    {"up": _book(0.05, 0.06), "down": _book(0.93, 0.95)},
    {"up": _book(0.04, 0.05), "down": _book(0.94, 0.96)},
    {"up": _book(0.04, 0.05), "down": _book(0.94, 0.96)},
    {"up": _book(0.75, 0.78), "down": _book(0.18, 0.20)},
    {"up": _book(0.75, 0.78), "down": _book(0.18, 0.20)},
    {"up": _book(0.75, 0.78), "down": _book(0.18, 0.20)},
]
CYCLE_STEP_S = 3.5  # matches the scripted 3.5s spans in tests/test_tick_lock_short.py


def measure(short: bool, scenario: str = "cycle", ticks: int = DEFAULT_TICKS) -> dict:
    harness = Harness(short=short, sniper=(scenario == "sniper"))
    sink = _Sink()
    harness.strategy.io.log = sink.log
    harness.strategy.io.alert = sink.alert
    timed = _TimedLock(harness.runner._tick_lock)
    harness.runner._tick_lock = timed
    orders = 0
    trigger_holds: list[float] = []
    for n in range(ticks):
        began = len(timed.holds)
        if scenario == "sniper":
            now = 1200.0 + n * TICK_S
            # Each iteration is a fresh feed trigger on a fresh market.
            harness.strategy.state["markets"]["cid"].pop("sniper_5m", None)
            harness.grid.add(now * 1000.0 - 400.0, 70_000.0, source="primary",
                             recv_ms=now * 1000.0 - 400.0, recv_wall_ms=now * 1000.0 - 400.0)
            harness.grid.add(now * 1000.0, 70_030.0, source="primary",
                             recv_ms=now * 1000.0, recv_wall_ms=now * 1000.0)
            harness.clock[0] = now
            harness.quotes = {"up": _book(0.92, 0.94, 20.0), "down": _book(0.06, 0.08, 20.0)}
            harness.runner.current = harness.strategy.state["markets"]["cid"]
            before = len(harness.buys)
            harness.feed_tick()
            orders += len(harness.buys) - before
            trigger_holds.extend(timed.holds[began:])
        else:
            # A fresh market per cycle, so every cycle really enters, holds and dumps.
            base = 1200.0 + n * len(CYCLE) * CYCLE_STEP_S
            harness.reset_market(base, ttm_s=len(CYCLE) * CYCLE_STEP_S)
            for idx, quotes in enumerate(CYCLE):
                harness.step(base + idx * CYCLE_STEP_S, quotes)
            orders += len(harness.buys) + len(harness.sells)
    holds = timed.holds
    if not holds:
        raise RuntimeError("no lock holds recorded")
    return {
        "scenario": scenario,
        "tick_lock_short": short,
        "ticks": ticks,
        "holds": len(holds),
        "hold_p50_us": statistics.median(holds),
        "hold_p90_us": _percentile(holds, 0.90),
        "hold_p99_us": _percentile(holds, 0.99),
        "hold_max_us": max(holds),
        "hold_total_ms": sum(holds) / 1000.0,
        "sink_total_ms": sink.ns / 1e6,
        "journal_lines": len(sink.lines),
        "journal_bytes": sum(len(line) for line in sink.lines),
        "orders": orders,
        "trigger_p50_us": statistics.median(trigger_holds) if trigger_holds else 0.0,
        "trigger_p99_us": _percentile(trigger_holds, 0.99) if trigger_holds else 0.0,
        "trigger_max_us": max(trigger_holds) if trigger_holds else 0.0,
    }


def _merge(rows: list[dict]) -> dict:
    """Median-of-repeats for every scalar; the rows are the same configuration."""
    keys = [k for k, v in rows[0].items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    out = {k: v for k, v in rows[0].items() if k not in keys}
    out["repeats"] = len(rows)
    for key in keys:
        out[key] = round(statistics.median(row[key] for row in rows), 3)
    return out


def run(ticks: int = DEFAULT_TICKS, repeats: int = DEFAULT_REPEATS) -> list[dict]:
    rows = []
    for scenario in ("cycle", "sniper"):
        for short in (False, True):
            rows.append(_merge([measure(short, scenario, ticks) for _ in range(repeats)]))
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--json", help="write the results here as JSON")
    args = parser.parse_args(argv)

    rows = run(args.ticks, args.repeats)
    header = (f"{'scenario':>8} {'mode':>16} {'holds':>6} {'held p50':>9} {'held p90':>9} "
              f"{'held p99':>9} {'held max':>9} {'held total':>11} {'sink total':>11} "
              f"{'lines':>6} {'orders':>7} {'trig p50':>9} {'trig p99':>9}")
    print(header + "   (us unless marked)")
    for row in rows:
        label = "tick_lock_short" if row["tick_lock_short"] else "default"
        trigger = (f" {row['trigger_p50_us']:>9.1f} {row['trigger_p99_us']:>9.1f}"
                   if row["scenario"] == "sniper" else f" {'-':>9} {'-':>9}")
        print(f"{row['scenario']:>8} {label:>16} {row['holds']:>6} "
              f"{row['hold_p50_us']:>9.1f} {row['hold_p90_us']:>9.1f} "
              f"{row['hold_p99_us']:>9.1f} {row['hold_max_us']:>9.1f} "
              f"{row['hold_total_ms']:>10.2f}ms {row['sink_total_ms']:>10.2f}ms "
              f"{row['journal_lines']:>6.0f} {row['orders']:>7.0f}{trigger}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
