"""Status command: python -m mdrecorder.status --out DIR."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .common import tree_bytes


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="mdrecorder.status", description="Show mdrecorder status"
    )
    parser.add_argument("--out", required=True, help="recorder output directory")
    return parser.parse_args(argv)


def _human(n):
    if n is None:
        return "?"
    step = 1024.0
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < step or unit == "TiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= step
    return f"{value:.1f}TiB"


def _age(seconds):
    if seconds is None:
        return "-"
    return f"{seconds:.1f}s"


def main(argv=None):
    args = _parse_args(argv)
    path = os.path.join(os.path.abspath(args.out), "status.json")
    if not os.path.exists(path):
        print(f"no status.json at {path}", file=sys.stderr)
        return 2
    with open(path, encoding="utf-8") as fh:
        snap = json.load(fh)

    print(f"out:      {snap.get('out')}")
    print(f"uptime:   {_age(snap.get('uptime_s'))}")
    print(f"records:  {snap.get('records_written')}  queue: {snap.get('queue')}"
          f"  drops: {snap.get('drops')}")

    streams = snap.get("streams") or {}
    widths = {
        "stream": max([len("stream")] + [len(s) for s in streams]),
        "msgs": 8,
        "age": 8,
        "reconnects": 6,
        "errors": 6,
    }
    header = (
        f"{'stream':<{widths['stream']}}  {'msgs':>{widths['msgs']}}  "
        f"{'last_age':>{widths['age']}}  {'reconn':>{widths['reconnects']}}  "
        f"{'errors':>{widths['errors']}}  state"
    )
    print()
    print(header)
    print("-" * len(header))
    for name in sorted(streams):
        row = streams[name]
        state = "up" if row.get("up") else "down"
        print(
            f"{name:<{widths['stream']}}  {row.get('msgs', 0):>{widths['msgs']}}  "
            f"{_age(row.get('last_msg_age_s')):>{widths['age']}}  "
            f"{row.get('reconnects', 0):>{widths['reconnects']}}  "
            f"{row.get('errors', 0):>{widths['errors']}}  {state}"
        )

    markets = snap.get("markets") or {}
    if markets:
        print()
        print("markets:")
        for slug in sorted(markets):
            print(f"  {slug}  {markets[slug].get('conditionId')}")

    disk = snap.get("disk_bytes")
    cap = snap.get("cap_bytes") or 0
    used = tree_bytes(os.path.abspath(args.out))
    print()
    print(f"disk: {_human(used)} on disk"
          + (f"  cap {_human(cap)}  ({100.0 * used / cap:.1f}%)" if cap else ""))
    if disk is not None and disk != used:
        print(f"      status.json reported {_human(disk)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
