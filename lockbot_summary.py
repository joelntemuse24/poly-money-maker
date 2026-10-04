#!/usr/bin/env python3
"""Print running lockbot P&L from logs/lockbot.jsonl.

Groups settlement rows by strategy (s1 NIULAI4, s2 Binance sniper)
and by asset/duration. Also prints signal and paper-fill counts,
decision-to-post latency, and the head-to-head wallet comparison
(median and p90 of our signal time minus their fill time, the share
of cases where our signal was first, and the price difference).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from buy.lock_report import format_summary, load_jsonl, summarize


ROOT = Path(__file__).resolve().parent
DEFAULT_LOG = ROOT / "logs" / "lockbot.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lockbot P&L summary")
    parser.add_argument("log", nargs="?", default=str(DEFAULT_LOG))
    args = parser.parse_args(argv)
    path = Path(args.log)
    if not path.exists():
        print(f"no log at {path}")
        return 1
    print(format_summary(summarize(load_jsonl(path))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
