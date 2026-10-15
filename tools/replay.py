#!/usr/bin/env python3
"""Replay a bag tape: book depth and a market-sell walk.

    python tools/replay.py depth TAPE --ts 1790000000 --side bid --price 0.92
    python tools/replay.py fill TAPE --ts 1790000000 --side sell --shares 100
    python tools/replay.py scenario TAPE --ts 1790000000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.replay import Tape, up_bid_scenario  # noqa: E402


def _tape(path: str) -> Tape:
    return Tape.load(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay one recorded BTC 15m bag")
    sub = parser.add_subparsers(dest="cmd", required=True)

    depth = sub.add_parser("depth", help="shares at or better than a price")
    depth.add_argument("tape")
    depth.add_argument("--ts", type=float, required=True)
    depth.add_argument("--leg", default="up")
    depth.add_argument("--side", default="bid", choices=("bid", "ask"))
    depth.add_argument("--price", type=float, required=True)
    depth.add_argument("--clock", default="recv", choices=("recv", "exch"))

    exact = sub.add_parser("size", help="shares resting on one price rung")
    exact.add_argument("tape")
    exact.add_argument("--ts", type=float, required=True)
    exact.add_argument("--leg", default="up")
    exact.add_argument("--side", default="bid", choices=("bid", "ask"))
    exact.add_argument("--price", type=float, required=True)
    exact.add_argument("--clock", default="recv", choices=("recv", "exch"))

    fill = sub.add_parser("fill", help="walk the book for a market order")
    fill.add_argument("tape")
    fill.add_argument("--ts", type=float, required=True)
    fill.add_argument("--leg", default="up")
    fill.add_argument("--side", default="sell", choices=("sell", "buy"))
    fill.add_argument("--shares", type=float, required=True)
    fill.add_argument("--clock", default="recv", choices=("recv", "exch"))

    scenario = sub.add_parser("scenario", help="Up bids >= 0.92 and a 100-share sell")
    scenario.add_argument("tape")
    scenario.add_argument("--ts", type=float, required=True)
    scenario.add_argument("--price", type=float, default=0.92)
    scenario.add_argument("--shares", type=float, default=100.0)
    scenario.add_argument("--clock", default="recv", choices=("recv", "exch"))

    args = parser.parse_args(argv)
    tape = _tape(args.tape)
    if args.cmd == "depth":
        value = tape.depth_at(args.ts, args.side, args.price, leg=args.leg, clock=args.clock)
        print(json.dumps({"depth": value, "side": args.side, "price": args.price, "leg": args.leg}))
        return 0
    if args.cmd == "size":
        value = tape.size_at(args.ts, args.side, args.price, leg=args.leg, clock=args.clock)
        print(json.dumps({"size": value, "side": args.side, "price": args.price, "leg": args.leg}))
        return 0
    if args.cmd == "fill":
        estimate = tape.fill_estimate(args.ts, args.side, args.shares, leg=args.leg, clock=args.clock)
        print(
            json.dumps(
                {
                    "filled": estimate.filled,
                    "remaining": estimate.remaining,
                    "vwap": estimate.vwap,
                    "notional": estimate.notional,
                    "levels": [list(level) for level in estimate.levels],
                }
            )
        )
        return 0
    print(json.dumps(up_bid_scenario(tape, args.ts, price=args.price, shares=args.shares, clock=args.clock)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
