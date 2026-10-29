#!/usr/bin/env python3
"""Synthetic GIL-handoff benchmark for ``thread_switch_interval_s``.

Layout (one process, one core's worth of work):

* ``publisher`` - a pure-Python CPU loop, i.e. a thread that never blocks and so
                  holds the GIL whenever it is running. Every ``gap`` iterations
                  it stamps the clock and sets a ``threading.Event``.
* ``feed``      - blocks on that event, exactly like a websocket thread blocked
                  in ``recv()``.

The measured value is **one-way**: publisher stamps -> feed thread runs. A
thread that already holds the GIL cannot be preempted faster than the switch
interval, so the interval shows up directly in this number. The publisher keeps
running the loop after setting the event, so the feed thread must take the GIL
away from it.

Overhead is process CPU seconds per handoff, so the cost of a shorter interval
is visible instead of implied.

Usage:
    python tools/bench_thread_switch.py --spins 400
    python tools/bench_thread_switch.py --spins 400 --json /tmp/switch.json
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET_INTERVALS = (None, 0.005, 0.001, 0.0005)
DEFAULT_SPINS = 400
REPEATS = 3
GAP_ITERS = 4000
TIMEOUT_S = 0.5


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def measure_handoff(interval_s: Optional[float], spins: int = DEFAULT_SPINS) -> dict:
    """One-way publisher-stamp -> feed-thread latency, in microseconds."""
    previous = sys.getswitchinterval()
    if interval_s is not None:
        sys.setswitchinterval(float(interval_s))
    wake = threading.Event()
    ready = threading.Event()
    stop = threading.Event()
    published = [0]
    samples: list[float] = []

    def feed() -> None:
        ready.set()
        while not stop.is_set():
            if not wake.wait(TIMEOUT_S):
                continue
            samples.append((time.perf_counter_ns() - published[0]) / 1000.0)
            wake.clear()

    def publisher() -> None:
        total = 0
        tick = 0
        while not stop.is_set():
            for i in range(GAP_ITERS):
                total += i
            tick += 1
            if tick % 1 == 0:
                published[0] = time.perf_counter_ns()
                wake.set()
        del total

    thread = threading.Thread(target=feed, name="bench-feed", daemon=True)
    pub = threading.Thread(target=publisher, name="bench-publisher", daemon=True)
    cpu_before = time.process_time()
    try:
        thread.start()
        ready.wait(2.0)
        pub.start()
        deadline = time.perf_counter() + max(2.0, spins * 0.05)
        while len(samples) < spins and time.perf_counter() < deadline:
            time.sleep(0.002)
        stop.set()
        wake.set()
        pub.join(timeout=2.0)
        thread.join(timeout=2.0)
        cpu_s = time.process_time() - cpu_before
    finally:
        sys.setswitchinterval(previous)

    if not samples:
        raise RuntimeError("no handoff samples recorded")
    samples = samples[:spins]
    return {
        "interval_s": None if interval_s is None else float(interval_s),
        "samples": len(samples),
        "p50_us": round(statistics.median(samples), 2),
        "p90_us": round(_percentile(samples, 0.90), 2),
        "p99_us": round(_percentile(samples, 0.99), 2),
        "max_us": round(max(samples), 2),
        "mean_us": round(statistics.fmean(samples), 2),
        "cpu_us_per_handoff": round(cpu_s / len(samples) * 1e6, 3),
    }


WORKER_ITERS = 60_000
THROUGHPUT_S = 0.5


def measure_throughput(interval_s: Optional[float], *, duration_s: float = THROUGHPUT_S,
                       workers: int = 2) -> dict:
    """Pure-Python work two threads complete per wall second at one interval.

    More frequent switches cost throughput; this is the CPU price of the knob.
    """
    previous = sys.getswitchinterval()
    if interval_s is not None:
        sys.setswitchinterval(float(interval_s))
    stop = threading.Event()
    counts = [0] * workers

    def worker(slot: int) -> None:
        done = 0
        while not stop.is_set():
            for _ in range(WORKER_ITERS):
                pass
            done += WORKER_ITERS
        counts[slot] = done

    threads = [threading.Thread(target=worker, args=(n,), name=f"bench-cpu-{n}", daemon=True)
               for n in range(workers)]
    try:
        for thread in threads:
            thread.start()
        began = time.perf_counter()
        time.sleep(duration_s)
        elapsed = time.perf_counter() - began
        stop.set()
        for thread in threads:
            thread.join(timeout=2.0)
    finally:
        sys.setswitchinterval(previous)
    return {
        "interval_s": None if interval_s is None else float(interval_s),
        "wall_s": round(elapsed, 3),
        "iters_per_s": int(sum(counts) / elapsed),
    }


def run(spins: int = DEFAULT_SPINS, intervals=TARGET_INTERVALS) -> list[dict]:
    rows = []
    baseline = None
    for interval in intervals:
        repeats = [measure_handoff(interval, spins) for _ in range(REPEATS)]
        throughput = statistics.median(
            measure_throughput(interval)["iters_per_s"] for _ in range(REPEATS))
        if baseline is None:
            baseline = throughput
        rows.append({
            "interval_s": repeats[0]["interval_s"],
            "samples": sum(r["samples"] for r in repeats),
            "p50_us": round(statistics.median(r["p50_us"] for r in repeats), 2),
            "p90_us": round(statistics.median(r["p90_us"] for r in repeats), 2),
            "p99_us": round(statistics.median(r["p99_us"] for r in repeats), 2),
            "max_us": round(min(r["max_us"] for r in repeats), 2),
            "cpu_us_per_handoff": round(
                statistics.median(r["cpu_us_per_handoff"] for r in repeats), 3),
            "iters_per_s": int(throughput),
            "throughput_vs_default_pct": round(100.0 * (throughput / baseline - 1.0), 1),
        })
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spins", type=int, default=DEFAULT_SPINS,
                        help="handoffs sampled per interval")
    parser.add_argument("--json", help="write the table here as JSON")
    args = parser.parse_args(argv)

    rows = run(args.spins)
    print(f"{'interval':>10} {'p50 us':>9} {'p90 us':>9} {'p99 us':>9} "
          f"{'max us':>9} {'iters/s':>12} {'vs default':>11}")
    for row in rows:
        label = "default" if row["interval_s"] is None else f"{row['interval_s'] * 1000:.1f}ms"
        print(f"{label:>10} {row['p50_us']:>9} {row['p90_us']:>9} {row['p99_us']:>9} "
              f"{row['max_us']:>9} {row['iters_per_s']:>12} "
              f"{row['throughput_vs_default_pct']:>10.1f}%")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
