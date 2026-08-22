#!/usr/bin/env python3
"""Replay the 5m live tape from JSONL hours later (no orders).

The Rich console you stream is only in systemd journald (50 MB / 7 days).
That is gone after a vacuum. The durable tape is ``buybot5m.journal.jsonl``
(money-path events only) written by ``polybuybot5m``, with ``buybot5m.log``
as a fallback.

Usage (VM, repo root):
  python check_live_journal.py
  python check_live_journal.py --hours 5
  python check_live_journal.py --since 2026-08-22T03:16:00
  python check_live_journal.py --hours 8 --csv /tmp/tape.csv

Exact Rich console, only while journald still has it:
  journalctl -u polybuybot5m --since "5 hours ago" --no-pager
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from buy.live_journal import (
    default_journal_path,
    format_tape_line,
    iter_rotated_paths,
    load_journal_events,
    since_iso_hours_ago,
    summarize_tape,
)

REPO = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Replay the 5m live buy/hedge tape from JSON logs"
    )
    ap.add_argument(
        "--log",
        type=Path,
        default=None,
        help="JSONL path (default: buybot5m.journal.jsonl, else buybot5m.log)",
    )
    ap.add_argument(
        "--hours",
        type=float,
        default=5.0,
        help="How far back to print (default 5). Ignored when --since is set.",
    )
    ap.add_argument(
        "--since",
        default="",
        help="ISO timestamp cutoff (inclusive). Overrides --hours.",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Print the whole retained tape (no time cutoff).",
    )
    ap.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Optional CSV of the same rows (do not commit exports/).",
    )
    args = ap.parse_args()
    primary = args.log if args.log is not None else default_journal_path(REPO)
    paths = iter_rotated_paths(primary)
    if not paths:
        print(f"no log files matching {primary}")
        print(
            "On the VM the tape is buybot5m.journal.jsonl after the 5m restart "
            "that writes it. Older sessions: buybot5m.log / buybot5m.log.1"
        )
        return 1
    if args.all:
        since = ""
    elif args.since:
        since = args.since
    else:
        since = since_iso_hours_ago(args.hours)
    events = load_journal_events(paths, since=since)
    stats = summarize_tape(events)
    print(
        f"logs: {', '.join(p.name for p in paths)}\n"
        f"since: {since or '(all)'}\n"
        f"events: {stats['events']}\n"
        f"first: {stats['first_ts'] or '(none)'}\n"
        f"last: {stats['last_ts'] or '(none)'}"
    )
    counts = stats["counts"]
    if counts:
        print("\ncounts:")
        for name, n in counts.most_common():
            print(f"  {n:5d}  {name}")
    print("\ntape:")
    if not events:
        print("  (none in this window)")
    else:
        for event in events:
            print(format_tape_line(event))
    if args.csv is not None:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "ts",
            "event",
            "condition_id",
            "token_id",
            "leg",
            "reason",
            "bid",
            "ask",
            "size",
            "error",
            "line",
        ]
        with args.csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for event in events:
                row = {key: event.get(key, "") for key in fieldnames}
                row["line"] = format_tape_line(event)
                writer.writerow(row)
        print(f"\ncsv: {args.csv}")
    print(
        "\nRich console (only while journald still has it):\n"
        "  journalctl -u polybuybot5m --since \"5 hours ago\" --no-pager"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
