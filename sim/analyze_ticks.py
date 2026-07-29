"""Analyze tick data: after selling the loser, did the winner dip below threshold and recover?

Usage:
    .venv/bin/python -m sim.analyze_ticks --tag 15m-tick-study
    .venv/bin/python -m sim.analyze_ticks --tag 15m-tick-study --thresholds 0.70,0.75,0.80
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def load_results(tag: str) -> list[dict]:
    path = Path(f"sim_data/{tag}/results.jsonl")
    if not path.exists():
        print(f"No results found at {path}")
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_ticks(tag: str, condition_id: str) -> list[dict]:
    path = Path(f"sim_data/{tag}/ticks/{condition_id}.jsonl")
    if not path.exists():
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def analyze(tag: str, thresholds: list[float]) -> None:
    results = load_results(tag)
    sold_results = [r for r in results if r.get("sell_leg")]
    print(f"Total markets: {len(results)}, with sells: {len(sold_results)}")
    print()

    for thr in thresholds:
        false_hedge_count = 0
        caught_reversals = 0
        missed_reversals = 0
        no_dip_count = 0
        dip_recover_count = 0
        dip_and_stayed_count = 0

        for r in sold_results:
            sell_leg = r["sell_leg"]
            winner = r.get("winner")
            is_reversal = sell_leg == winner
            held_leg = "dn" if sell_leg == "up" else "up"

            ticks = load_ticks(tag, r["condition_id"])
            if not ticks:
                continue

            sell_ts = None
            for t in ticks:
                if t.get("seconds_left") and r.get("sell_seconds_left"):
                    if abs(t["seconds_left"] - r["sell_seconds_left"]) < 1.0:
                        sell_ts = t
                        break

            sell_sl = r.get("sell_seconds_left", 0)
            post_sell_ticks = [t for t in ticks if t.get("seconds_left", 999) < sell_sl - 0.5]

            held_bid_key = f"{held_leg}_bid"
            dipped = False
            recovered = False
            min_bid = 1.0

            for t in post_sell_ticks:
                bid = t.get(held_bid_key)
                if bid is None:
                    continue
                min_bid = min(min_bid, bid)
                if bid < thr:
                    dipped = True
                if dipped and bid >= thr + 0.05:
                    recovered = True

            if not dipped:
                no_dip_count += 1
                if is_reversal:
                    missed_reversals += 1
            elif recovered:
                dip_recover_count += 1
                false_hedge_count += 1
            else:
                dip_and_stayed_count += 1
                if is_reversal:
                    caught_reversals += 1
                else:
                    false_hedge_count += 1

        total = no_dip_count + dip_recover_count + dip_and_stayed_count
        if total == 0:
            print(f"Threshold {thr:.2f}: no tick data yet")
            continue

        print(f"=== Hedge threshold {thr:.2f} ===")
        print(f"  Winner never dipped below {thr:.0f}c:  {no_dip_count:>4} ({no_dip_count/total*100:.1f}%)")
        print(f"  Dipped below {thr:.0f}c, recovered:    {dip_recover_count:>4} ({dip_recover_count/total*100:.1f}%)  [FALSE HEDGE]")
        print(f"  Dipped below {thr:.0f}c, stayed low:    {dip_and_stayed_count:>4} ({dip_and_stayed_count/total*100:.1f}%)  [CORRECT HEDGE]")
        print(f"  Reversals caught:   {caught_reversals}")
        print(f"  Reversals missed:   {missed_reversals}")
        print(f"  False hedges:       {false_hedge_count}")
        print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="15m-tick-study", help="Data tag for sim data")
    parser.add_argument("--thresholds", default="0.70,0.75,0.80", help="Comma-separated hedge thresholds to test")
    args = parser.parse_args()

    thresholds = [float(x) for x in args.thresholds.split(",")]
    analyze(args.tag, thresholds)


if __name__ == "__main__":
    main()
