#!/usr/bin/env python3
"""Print legacy lockbot settlement P&L by stored strategy and market."""

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
