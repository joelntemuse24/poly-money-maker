#!/usr/bin/env python3
"""At T, how many Up bids are >= 0.92, and what would selling 100 average.

    python tools/example_up_bid.py data/tape/YYYY-MM-DD/<slug>.jsonl.gz --ts UNIX

``shares_bid_at_or_above`` is every bid at or above 0.92.
``shares_bid_at_price`` is the single 0.92 rung.
``sell_vwap`` is the share-weighted average of a market sell that walks
those bids from the top. It is the displayed book, not a queue model.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Up bid depth at 0.92 and a 100-share sell")
    parser.add_argument("tape")
    parser.add_argument("--ts", type=float, required=True)
    parser.add_argument("--price", type=float, default=0.92)
    parser.add_argument("--shares", type=float, default=100.0)
    parser.add_argument("--clock", default="recv", choices=("recv", "exch"))
    args = parser.parse_args(argv)
    report = up_bid_scenario(
        Tape.load(args.tape),
        args.ts,
        price=args.price,
        shares=args.shares,
        clock=args.clock,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
