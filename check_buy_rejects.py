#!/usr/bin/env python3
"""Count 5m markets that died on CLOB 'invalid amounts' (2 dp USDC / 4 dp shares).

Those buy_attempts passed every strategy gate. The POST never landed, so this
is the upper bound of 'we would have been in the market' if FAK sizing had
been exact cents. FAK could still have come back empty.

Usage (VM, repo root):
  python check_buy_rejects.py
  python check_buy_rejects.py --log buybot5m.log
"""

from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO = Path(__file__).resolve().parent
AMOUNT_NEEDLE = "max accuracy of 2 decimals"


def iter_log_paths(primary: Path) -> List[Path]:
    """Oldest rotated file first, then the live log."""
    paths: List[Path] = []
    for n in range(12, 0, -1):
        rotated = Path(f"{primary}.{n}")
        if rotated.is_file():
            paths.append(rotated)
    if primary.is_file():
        paths.append(primary)
    return paths


def load_events(paths: Iterable[Path]) -> List[dict]:
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
                if name in {
                    "buy_attempt",
                    "buy_attempt_rejected",
                    "buy_fill",
                    "buy_fail",
                    "buy_ghost_fill",
                }:
                    rows.append(event)
    rows.sort(key=lambda row: str(row.get("ts") or ""))
    return rows


def is_amount_reject(event: dict) -> bool:
    if event.get("event") != "buy_attempt_rejected":
        return False
    return AMOUNT_NEEDLE in str(event.get("error") or "")


def summarize(events: List[dict]) -> Dict[str, Any]:
    pending: Optional[dict] = None
    markets: "OrderedDict[str, dict]" = OrderedDict()

    def bucket(condition_id: str) -> dict:
        row = markets.get(condition_id)
        if row is None:
            row = {
                "condition_id": condition_id,
                "attempts": 0,
                "amount_rejects": 0,
                "fills": 0,
                "first_ts": None,
                "last_ts": None,
                "asks": [],
            }
            markets[condition_id] = row
        return row

    for event in events:
        name = event.get("event")
        ts = event.get("ts")
        if name == "buy_attempt":
            pending = event
            cid = str(event.get("condition_id") or "")
            if cid:
                row = bucket(cid)
                row["attempts"] += 1
                row["first_ts"] = row["first_ts"] or ts
                row["last_ts"] = ts
                ask = event.get("ask")
                if ask is not None:
                    row["asks"].append(ask)
            continue
        if is_amount_reject(event):
            cid = ""
            if pending and pending.get("condition_id"):
                cid = str(pending["condition_id"])
            cid = cid or str(event.get("token_id") or "unknown")
            row = bucket(cid)
            row["amount_rejects"] += 1
            row["first_ts"] = row["first_ts"] or ts
            row["last_ts"] = ts
            continue
        if name in {"buy_fill", "buy_ghost_fill"}:
            cid = str(event.get("condition_id") or "")
            if cid:
                row = bucket(cid)
                row["fills"] += 1
                row["last_ts"] = ts
            pending = None

    blocked = [
        row
        for row in markets.values()
        if row["amount_rejects"] > 0 and row["fills"] == 0
    ]
    filled_anyway = [
        row
        for row in markets.values()
        if row["amount_rejects"] > 0 and row["fills"] > 0
    ]
    return {
        "events": len(events),
        "blocked": blocked,
        "filled_anyway": filled_anyway,
        "reject_posts": sum(row["amount_rejects"] for row in markets.values()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Count markets that never bought because FAK amounts were 2.4999 USDC"
    )
    ap.add_argument("--log", type=Path, default=REPO / "buybot5m.log")
    args = ap.parse_args()
    paths = iter_log_paths(args.log)
    if not paths:
        print(f"no log files matching {args.log}")
        return 1
    stats = summarize(load_events(paths))
    blocked = stats["blocked"]
    print(
        f"logs: {', '.join(p.name for p in paths)}\n"
        f"CLOB 'invalid amounts' POSTs: {stats['reject_posts']}\n"
        f"unique markets blocked (attempted, never filled): {len(blocked)}\n"
        f"unique markets that rejected then later filled: {len(stats['filled_anyway'])}"
    )
    print(
        "This is an upper bound on 'would have been in': a valid FAK can still "
        "come back empty."
    )
    if blocked:
        print("\nts                       ask    rejects  condition_id")
        for row in blocked:
            asks = row["asks"]
            ask_s = f"{asks[0]:.2f}" if asks else "  —"
            print(
                f"{row['first_ts'] or '?':<24} {ask_s:>5}  "
                f"{row['amount_rejects']:>7}  {row['condition_id'][:16]}…"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
