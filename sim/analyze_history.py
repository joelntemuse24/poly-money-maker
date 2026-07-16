#!/usr/bin/env python3
"""Calibrate entry set-cost and loser-sell prices from a Polymarket history CSV export."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from typing import Dict, List, Tuple


def analyze(path: str) -> dict:
    markets: Dict[str, dict] = defaultdict(lambda: {"up": [], "dn": [], "sells": [], "redeems": []})
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            m = row.get("marketName") or ""
            if "Bitcoin Up or Down" not in m:
                continue
            action = row.get("action") or ""
            usdc = float(row.get("usdcAmount") or 0)
            tok = float(row.get("tokenAmount") or 0)
            name = (row.get("tokenName") or "").lower()
            if action == "Buy" and tok > 0:
                px = usdc / tok
                if name == "up":
                    markets[m]["up"].append((px, tok, usdc))
                elif name == "down":
                    markets[m]["dn"].append((px, tok, usdc))
            elif action == "Sell" and tok > 0:
                markets[m]["sells"].append((usdc / tok, tok, name, usdc))
            elif action == "Redeem":
                markets[m]["redeems"].append((usdc, tok))

    set_costs: List[float] = []
    for m, v in markets.items():
        if not v["up"] or not v["dn"]:
            continue
        up_c = sum(x[2] for x in v["up"])
        up_t = sum(x[1] for x in v["up"])
        dn_c = sum(x[2] for x in v["dn"])
        dn_t = sum(x[1] for x in v["dn"])
        if up_t > 0 and dn_t > 0:
            set_costs.append(up_c / up_t + dn_c / dn_t)

    loser_px = [s[0] for v in markets.values() for s in v["sells"] if s[0] < 0.40]
    all_sell_px = [s[0] for v in markets.values() for s in v["sells"]]

    def stats(xs: List[float]) -> dict:
        if not xs:
            return {}
        xs = sorted(xs)
        return {
            "n": len(xs),
            "mean": sum(xs) / len(xs),
            "p50": xs[len(xs) // 2],
            "p10": xs[max(0, len(xs) // 10)],
            "p90": xs[min(len(xs) - 1, 9 * len(xs) // 10)],
            "min": xs[0],
            "max": xs[-1],
        }

    return {
        "markets_seen": len(markets),
        "set_cost": stats(set_costs),
        "recommended_set_cost": (sum(set_costs) / len(set_costs)) if set_costs else 1.045,
        "loser_sells_lt_40c": stats(loser_px),
        "all_sells": stats(all_sell_px),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", help="Polymarket history export CSV")
    args = ap.parse_args()
    out = analyze(args.csv_path)
    print(json.dumps(out, indent=2))
    if out.get("recommended_set_cost"):
        print(f"\nSuggested sim.set_cost = {out['recommended_set_cost']:.4f}")


if __name__ == "__main__":
    main()
