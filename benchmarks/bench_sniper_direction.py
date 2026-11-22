#!/usr/bin/env python3
"""Microbenchmark: the sniper wrong-side (direction) guard and its open-price read.

Three measurements, all in microseconds per call:

* ``window_open_price``: a real ``PriceGrid`` Coinbase deque holding 50 ticks
  inside the 500ms window and 4096 ticks in total. The walk stops at the oldest
  in-window tick, so this is the guard's only cost beyond the closed-form jump
  maths.
* ``guard decision``: ``_sniper_direction_decision`` on its own — one call,
  including the open-price walk and the threshold/verdict work.
* ``_sniper_check``: the full scan with a real grid, with the direction check on
  and off. ``sniper_min_gap_usd`` is set high so the scan always rejects before
  an order POST and both variants stop at the same gate. The 500ms scan over
  4096 ticks dominates these loops, so the on/off delta is coarse; the two
  figures above are the guard's actual cost.

Usage:
    python benchmarks/bench_sniper_direction.py
    python benchmarks/bench_sniper_direction.py --iters 200000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for extra in (ROOT, os.path.join(ROOT, "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from buy.binance_trades import PriceGrid  # noqa: E402
from buy.fivemin import FiveMinIO, Strategy  # noqa: E402

import test_fivemin as helpers  # noqa: E402

BASE_S = 1_200.0
TTM_S = 100.0
WINDOW_MS = 500.0
TOTAL_TICKS = 4096
WINDOW_TICKS = 50

QUOTES = {
    "up": {"bid": .90, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"},
    "down": {"bid": .06, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"},
}


def seeded_grid(mono_ms: Optional[float] = None) -> tuple[PriceGrid, float, float]:
    """A Coinbase deque with ``TOTAL_TICKS`` ticks, the newest 50 in the window.

    The series is flat and the newest tick prints +$30, so one jump direction
    clears ``sniper_jump_usd`` and the direction guard runs its pass path. The
    high ``sniper_min_gap_usd`` then rejects before an order POST.
    ``mono_ms`` is the monotonic reference the recv stamps sit on; pass the same
    value the scan will see (it anchors the window on ``time.monotonic()``).
    """
    grid = PriceGrid()
    now_mono = time.monotonic() * 1000.0 if mono_ms is None else float(mono_ms)
    # The scan ages its trigger against the primary feed's exchange stamp, which
    # sits on the ``_sniper_check`` clock (``BASE_S`` seconds); the Coinbase
    # window and its wall stamps share that epoch as well.
    base_ms = BASE_S * 1000.0
    grid.add(base_ms - 200.0, 70000.0, recv_ms=now_mono - 200.0,
             recv_wall_ms=base_ms - 200.0)
    # Ticks 10ms apart, so the 500ms window holds 50 of the 4096 buffered ones.
    for step in range(TOTAL_TICKS, 0, -1):
        offset = float(step) * 10.0
        px = 70000.0 + (0.05 if step % 7 == 0 else 0.0)
        grid.add(base_ms - offset, px, source="coinbase",
                 recv_ms=now_mono - offset, recv_wall_ms=base_ms - offset)
    # Appended last with the smallest offset, so this is the deque's newest tick.
    grid.add(base_ms - 5.0, 70030.0, source="coinbase", recv_ms=now_mono - 5.0,
             recv_wall_ms=base_ms - 5.0)
    return grid, now_mono, base_ms


def bench_window_open(grid: PriceGrid, iters: int) -> float:
    """One open-price walk per call, over the newest 500ms of the Coinbase deque."""
    upper = grid._recv_ticks_by_source["coinbase"][-1][0]
    lower = upper - WINDOW_MS
    fn = grid.window_open_price
    start = time.perf_counter()
    for _ in range(iters):
        fn("coinbase", lower, upper)
    return (time.perf_counter() - start) / max(1, iters)


def make_case() -> tuple[Strategy, dict, dict]:
    io = FiveMinIO(
        buy=lambda token, shares, limit, dry: {"shares": shares, "cost": shares * limit,
                                               "price": limit, "status": "matched"},
        sell=lambda *a: {}, reserves=lambda cfg: 0, redeem=lambda *a: None,
        save=lambda state: None, log=lambda event, **kw: None,
        alert=lambda *a, **kw: None, clock=lambda: BASE_S,
    )
    strategy = Strategy(io, startup_ts=0)
    strategy.add_market(helpers.market(), now=1000)
    row = strategy.state["markets"]["cid"]
    row["strike"] = 70000.0
    row["offset"] = 0.0
    row["end"] = BASE_S + TTM_S
    strategy.sample_cash(BASE_S, 1000)
    config = helpers.cfg(sniper_5m_enabled=True, sniper_use_offset=True,
                         sniper_gap_estimator="legacy",
                         sniper_min_gap_usd=1000.0)  # always reject: no order POST noise
    return strategy, config, row


def bench_check(strategy: Strategy, config: dict, row: dict, grid: PriceGrid,
                iters: int, *, warmup: int = 0) -> float:
    """Time the full scan against a grid pinned to the scan's own clock.

    ``_sniper_check`` anchors its window on ``time.monotonic()`` and rejects a
    trigger whose exchange stamp is older than ``fivemin_btc_max_age_s``, so a
    grid seeded at process start goes stale after a couple of seconds of
    looping and every later iteration would exit before the gates. Freezing the
    monotonic clock at the seed time keeps both checks on the same reference for
    the whole run; the docstring of the tool says so, and the loop cost is the
    scan itself, which is what this measures.
    """
    check = strategy._sniper_check
    base_ms = BASE_S * 1000.0
    tick = [1]

    def step(prefix: str) -> None:
        tick[0] += 1
        row.pop("sniper_5m", None)
        row["slug"] = "%s-%d" % (prefix, tick[0] & 0x3F)
        check(config, BASE_S, row, QUOTES, grid, TTM_S, defer_buy=True)

    for _ in range(warmup):
        step("warm")
    start = time.perf_counter()
    for _ in range(iters):
        step("bench")
    return (time.perf_counter() - start) / max(1, iters)


def bench_guard(strategy: Strategy, config: dict, info: dict, grid: PriceGrid,
                iters: int) -> float:
    """The guard on its own: one decision call plus the cached open-price walk."""
    decide = strategy._sniper_direction_decision
    start = time.perf_counter()
    for _ in range(iters):
        decide(config, grid, info, 1.0, {})
    return (time.perf_counter() - start) / max(1, iters)


def best_of(fn, repeat: int) -> float:
    return min(fn() for _ in range(max(1, repeat)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iters", type=int, default=20_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    grid, _mono, _wall = seeded_grid()
    warmup = max(1, args.iters // 10)
    open_us = best_of(lambda: (bench_window_open(grid, warmup),
                               bench_window_open(grid, args.iters))[1], args.repeat)

    # Pin the monotonic clock the scan anchors on to the grid's own recv stamps,
    # so every iteration reaches the gates instead of expiring on staleness.
    frozen_mono = time.monotonic()

    strategy, config, row = make_case()
    config_on = dict(config, sniper_jump_direction_check=True)
    config_off = dict(config, sniper_jump_direction_check=False)
    # Both variants share one strategy, and the first scan of a process pays
    # import and attribute caches; warm up before either is measured.
    bench_check(strategy, config_on, row, grid, 2 * warmup)
    with patch("buy.fivemin.time.monotonic", return_value=frozen_mono):
        on_us = best_of(lambda: bench_check(strategy, config_on, row, grid, args.iters,
                                            warmup=warmup), args.repeat)
        off_us = best_of(lambda: bench_check(strategy, config_off, row, grid, args.iters,
                                             warmup=warmup), args.repeat)

    # The open-price walk itself, on the newest 500ms of a deque seeded against
    # the same frozen monotonic reference, so the walk covers 50 ticks.
    guard_grid, _, _ = seeded_grid(frozen_mono * 1000.0)
    info = guard_grid.best_jump_in_window(WINDOW_MS, now_ms=BASE_S * 1000.0,
                                          recv_now_ms=frozen_mono * 1000.0)
    guard_us = best_of(lambda: bench_guard(strategy, config_on, info, guard_grid, args.iters),
                       args.repeat)

    print(f"window_open_price: {open_us * 1e6:8.3f} us/call  "
          f"({WINDOW_TICKS} in-window / {TOTAL_TICKS} total ticks, "
          f"{args.iters} iters x{args.repeat})")
    print(f"guard decision   : {guard_us * 1e6:8.3f} us/call  (walk + verdict)")
    print(f"_sniper_check on : {on_us * 1e6:8.3f} us/check")
    print(f"_sniper_check off: {off_us * 1e6:8.3f} us/check")
    # The 500ms jump scan over 4096 ticks dominates a scan and sits well above
    # the guard, so this delta is only readable at coarse resolution.
    print(f"delta            : {(on_us - off_us) * 1e6:+8.3f} us/check (on - off)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"window_open_price_us": open_us * 1e6,
                       "guard_decision_us": guard_us * 1e6,
                       "check_on_us": on_us * 1e6,
                       "check_off_us": off_us * 1e6}, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
