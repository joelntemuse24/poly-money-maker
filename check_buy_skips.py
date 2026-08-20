#!/usr/bin/env python3
"""Summarize why the 5m bot refused or failed to buy, from JSON logs.

Past logs only contain skips that were actually written. Before the
throttled ``buy_skip`` / ``buy_window`` events, most in-window "ask not
in 75–90" ticks were silent — those cannot be recovered.

Usage (VM, repo root):
  python check_buy_skips.py
  python check_buy_skips.py --since 2026-08-19T09:42:23
  python check_buy_skips.py --log buybot5m.log
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO = Path(__file__).resolve().parent

SKIP_EVENTS = {
    "buy_skip",
    "buy_skip_ambiguous",
    "buy_skip_no_consensus",
    "buy_skip_incomplete_book",
    "buy_skip_underlying_edge",
    "buy_skip_underlying_side",
    "buy_skip_max_positions",
    "buy_skip_max_notional",
    "buy_skip_max_daily_notional",
    "buy_skip_balance",
    "buy_window",
    "buy_attempt",
    "buy_attempt_rejected",
    "buy_fail",
    "buy_fill",
    "buy_ghost_fill",
    "buy_success",
}

FAULT_EVENTS = {"cycle_error"}


def iter_log_paths(primary: Path) -> List[Path]:
    paths: List[Path] = []
    for n in range(12, 0, -1):
        rotated = Path(f"{primary}.{n}")
        if rotated.is_file():
            paths.append(rotated)
    if primary.is_file():
        paths.append(primary)
    return paths


def load_events(paths: Iterable[Path], since: str = "") -> List[dict]:
    rows: List[dict] = []
    for path in paths:
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                name = event.get("event")
                if name not in SKIP_EVENTS and name not in FAULT_EVENTS:
                    continue
                ts = str(event.get("ts") or "")
                if since and ts < since:
                    continue
                rows.append(event)
    rows.sort(key=lambda row: str(row.get("ts") or ""))
    return rows


def cycle_error_label(event: dict) -> str:
    """Prefer the structured ``error`` field; fall back to the last traceback line."""
    err = str(event.get("error") or "").strip()
    if err:
        return err[:180]
    tb = str(event.get("traceback") or "").strip()
    if not tb:
        return "(empty)"
    return tb.splitlines()[-1][:180]


def skip_reason(event: dict) -> Optional[str]:
    name = str(event.get("event") or "")
    if name == "buy_skip":
        return str(event.get("reason") or "unspecified")
    if name.startswith("buy_skip_"):
        return name[len("buy_skip_") :]
    if name == "buy_attempt_rejected":
        err = str(event.get("error") or "")
        if "max accuracy of 2 decimals" in err:
            return "invalid_amount"
        return "rejected"
    if name == "buy_fail":
        return f"fail_{event.get('status') or 'empty'}"
    return None


def summarize(events: List[dict]) -> Dict[str, Any]:
    reasons = Counter()
    cycle_errors = Counter()
    windows = 0
    attempts = 0
    fills = 0
    markets: "OrderedDict[str, dict]" = OrderedDict()

    def bucket(cid: str) -> dict:
        row = markets.get(cid)
        if row is None:
            row = {
                "condition_id": cid,
                "windows": 0,
                "attempts": 0,
                "fills": 0,
                "reasons": Counter(),
                "first_ts": None,
                "last_ts": None,
            }
            markets[cid] = row
        return row

    for event in events:
        name = event.get("event")
        ts = event.get("ts")
        if name == "cycle_error":
            cycle_errors[cycle_error_label(event)] += 1
            continue
        cid = str(event.get("condition_id") or event.get("token_id") or "")
        reason = skip_reason(event)
        if cid:
            row = bucket(cid)
            row["first_ts"] = row["first_ts"] or ts
            row["last_ts"] = ts
            if reason:
                row["reasons"][reason] += 1
        if name == "buy_window":
            windows += 1
            if cid:
                bucket(cid)["windows"] += 1
        elif name == "buy_attempt":
            attempts += 1
            if cid:
                bucket(cid)["attempts"] += 1
        elif name in {"buy_fill", "buy_ghost_fill", "buy_success"}:
            fills += 1
            if cid:
                bucket(cid)["fills"] += 1
        if reason:
            reasons[reason] += 1
    return {
        "events": len(events),
        "windows": windows,
        "attempts": attempts,
        "fills": fills,
        "reasons": reasons,
        "cycle_errors": cycle_errors,
        "markets": list(markets.values()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Count 5m buy skips / attempts / fills from JSON logs"
    )
    ap.add_argument("--log", type=Path, default=REPO / "buybot5m.log")
    ap.add_argument("--since", default="", help="ISO timestamp cutoff (inclusive)")
    args = ap.parse_args()
    paths = iter_log_paths(args.log)
    if not paths:
        print(f"no log files matching {args.log}")
        return 1
    stats = summarize(load_events(paths, since=args.since))
    print(
        f"logs: {', '.join(p.name for p in paths)}\n"
        f"since: {args.since or '(all)'}\n"
        f"cycle_error: {sum(stats['cycle_errors'].values())}\n"
        f"buy_window (entered last 120s): {stats['windows']}\n"
        f"buy_attempt: {stats['attempts']}\n"
        f"fills: {stats['fills']}\n"
        f"unique markets with any skip/attempt: {len(stats['markets'])}"
    )
    if stats["cycle_errors"]:
        print("\ncycle_error types (per-market in hedge/buy; outer still aborts pre/post-market work):")
        for label, n in stats["cycle_errors"].most_common(15):
            print(f"  {n:5d}  {label}")
    print(
        "\nSilent pre-patch ticks (not in last 120s, or ask out of band "
        "before buy_skip logging) cannot be recovered."
    )
    print("\nreason counts:")
    if not stats["reasons"]:
        print("  (none)")
    else:
        for reason, n in stats["reasons"].most_common():
            print(f"  {n:5d}  {reason}")
    skipped = [m for m in stats["markets"] if m["attempts"] == 0 and m["fills"] == 0]
    if skipped:
        print(f"\nmarkets that never attempted ({len(skipped)}), last reason:")
        for row in skipped[-20:]:
            if not row["reasons"]:
                last_r = "window_only"
            else:
                last_r = row["reasons"].most_common(1)[0][0]
            print(
                f"  {row['last_ts'] or '?'}  {last_r:<20}  "
                f"{row['condition_id'][:18]}…"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
