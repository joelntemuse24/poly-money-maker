#!/usr/bin/env python3
"""Microbenchmark: the pre-order Chainlink side guard in the 5m sniper.

Each variant calls the same ``Strategy._sniper_check`` -> ``_sniper_scan`` with
a fake grid, a fake ``rtds_feed._live`` and a stubbed ``io.buy``. The min-gap
gate is set high on purpose so every iteration stops before an order POST (and
at the same gate in every variant); the measured work is the scan plus whatever
the CL guard adds:

* ``raw``: one ``_live`` read and the age check.
* ``projected``: raw plus one latest-tick read and one price-at lookup.
* ``twap``: the last-minute settlement average. A real ``RtdsTwapFeed`` holds
  90s of recorded Chainlink prints, and the check runs at 30s to expiry, so the
  guard walks the elapsed half of the averaging window each call. This is the
  worst case: 60s of ticks.

Usage:
    python benchmarks/bench_sniper_cl_guard.py
    python benchmarks/bench_sniper_cl_guard.py --iters 200000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for extra in (ROOT, os.path.join(ROOT, "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from buy.fivemin import FiveMinIO, Strategy  # noqa: E402
from buy.oracle_log import RtdsTwapFeed  # noqa: E402
import test_fivemin as helpers  # noqa: E402

BASE_S = 1_200.0
NOW_MS = BASE_S * 1000.0
TTM_S = 30.0


class FakeGrid:
    """Only the reads ``_sniper_scan`` and the CL guard perform."""

    def __init__(self) -> None:
        self.info = {"spot": 70060.0, "min": 70030.0, "max": 70060.0, "up_jump": 30.0,
                     "down_jump": 30.0, "ts_ms": NOW_MS, "recv_wall_ms": NOW_MS,
                     "source": "primary"}
        self.feeds = {"coinbase": (NOW_MS, NOW_MS, 70015.0)}

    def best_jump_in_window(self, window_ms=500.0, now_ms=None, *, sources=None, recv_now_ms=None):
        return self.info

    def latest_exchange_ms(self):
        return NOW_MS

    def latest_source_tick(self, source="primary"):
        return self.feeds.get(source)

    def source_price_at(self, source, target_ms, *, window_ms=60000.0):
        return 70010.0 if source == "coinbase" else None


QUOTES = {
    "up": {"bid": .90, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"},
    "down": {"bid": .06, "bids": [], "asks": [], "age_ms": 12.0, "src": "ws"},
}


def seeded_feed(now: float) -> RtdsTwapFeed:
    """90s of one-per-second Chainlink prints, the retention mintbot uses."""
    feed = RtdsTwapFeed(history_s=90.0, clock=lambda: now)
    for step in range(90, 0, -1):
        obs_ms = int(round((now - step) * 1000.0))
        feed.handle_message(json.dumps({
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "timestamp": obs_ms,
            "payload": {"symbol": "btc/usd", "timestamp": obs_ms,
                        "value": 70020.0 + (step % 5)},
        }))
    return feed


def make_case(guard: str) -> tuple[Strategy, dict, dict, FakeGrid]:
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
    # Put the market end inside the last averaging minute so the twap variant
    # takes the settlement path on every call.
    row["end"] = BASE_S + TTM_S
    strategy.sample_cash(BASE_S, 1000)
    # The guard ages the print against ``time.time()``, so seed a real wall stamp.
    wall = time.time()
    live = SimpleNamespace(price="70020.0", obs_ts=wall - 1.0, recv_ts=wall - 1.0)
    strategy.rtds_feed = SimpleNamespace(_live=live)
    if guard == "twap":
        feed = seeded_feed(wall)
        feed._live = live  # the scan reads ``_live`` off the same feed object
        strategy.rtds_feed = feed
    config = helpers.cfg(sniper_5m_enabled=True, sniper_use_offset=True,
                         sniper_gap_estimator="legacy",
                         sniper_min_gap_usd=1000.0)  # always reject: no order POST noise
    config["sniper_cl_guard"] = guard != "off"
    if guard != "off":
        config["sniper_cl_guard_mode"] = "raw"
    config["sniper_cl_twap_guard"] = guard == "twap"
    return strategy, config, row, FakeGrid()


def run(strategy: Strategy, config: dict, row: dict, grid: FakeGrid, iters: int) -> float:
    check = strategy._sniper_check
    start = time.perf_counter()
    for i in range(iters):
        # Clearing the attempt flag makes every iteration re-run the gates. The
        # slug cycles through a few keys so reject bookkeeping stays in the cache.
        row.pop("sniper_5m", None)
        row["slug"] = "bench-%d" % (i & 0x3F)
        check(config, BASE_S, row, QUOTES, grid, TTM_S, defer_buy=True)
    elapsed = time.perf_counter() - start
    return elapsed / max(1, iters)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iters", type=int, default=100_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    variants = ("off", "raw", "projected", "twap")
    out = {}
    for guard in variants:
        best = None
        for _ in range(max(1, args.repeat)):
            strategy, config, row, grid = make_case(guard)
            score = run(strategy, config, row, grid, args.iters)
            best = score if best is None else min(best, score)
        out[guard] = best
        print(f"{guard:>9}: {best * 1e6:8.3f} us/check  ({args.iters} iters x{args.repeat})")
    for guard in variants[1:]:
        if out["off"] and out[guard]:
            delta = (out[guard] - out["off"]) * 1e6
            print(f"delta {guard:>9}: {delta:+8.3f} us/check ({guard} - off)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({k: v * 1e6 for k, v in out.items()}, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
