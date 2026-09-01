"""Replay helpers for the 5m live tape (no I/O besides reading JSONL).

The Rich console is only in systemd journald (50 MB / 7 day cap). The
durable source is a small rotating tape of money-path events, plus the
noisier ``buybot5m.log`` as a fallback.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

JOURNAL_PREFIXES = ("buy_", "hedge_", "sell_", "redeem_", "dry_", "complement_")
JOURNAL_EXACT = frozenset(
    {
        "cycle_error",
        "pnl_recorded",
        "gc",
        "tick_size_lookup_fail",
    }
)

_DETAIL_KEYS = (
    "reason",
    "leg",
    "slice",
    "band",
    "bid",
    "ask",
    "size",
    "filled",
    "spent",
    "price",
    "price_limit",
    "from_tick",
    "to_tick",
    "error",
    "net",
    "outcome",
    "persist_why",
    "via",
    "status",
)


def is_journal_event(name: object) -> bool:
    event = str(name or "")
    if not event:
        return False
    if event in JOURNAL_EXACT:
        return True
    return event.startswith(JOURNAL_PREFIXES)


def since_iso_hours_ago(hours: float, *, now: Optional[datetime] = None) -> str:
    clock = now if now is not None else datetime.now()
    return (clock - timedelta(hours=float(hours))).isoformat()


def iter_rotated_paths(primary: Path, backups: int = 16) -> List[Path]:
    """Oldest rotation first, then the live file — same order as written."""
    paths: List[Path] = []
    for n in range(int(backups), 0, -1):
        rotated = Path(f"{primary}.{n}")
        if rotated.is_file():
            paths.append(rotated)
    if primary.is_file():
        paths.append(primary)
    return paths


def default_journal_path(repo: Path) -> Path:
    """Prefer the dedicated tape; fall back to the noisy bot log."""
    tape = repo / "buybot5m.journal.jsonl"
    if tape.is_file() or Path(f"{tape}.1").is_file():
        return tape
    return repo / "buybot5m.log"


def load_journal_events(
    paths: Iterable[Path],
    *,
    since: str = "",
    include_all: bool = False,
) -> List[dict]:
    rows: List[dict] = []
    cutoff = str(since or "")
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
                if not include_all and not is_journal_event(name):
                    continue
                ts = str(event.get("ts") or "")
                if cutoff and ts < cutoff:
                    continue
                rows.append(event)
    rows.sort(key=lambda row: str(row.get("ts") or ""))
    return rows


def format_tape_line(event: dict) -> str:
    ts = str(event.get("ts") or "?")
    name = str(event.get("event") or "?")
    details: List[str] = []
    cid = str(event.get("condition_id") or "")
    if cid:
        details.append(cid[:16] + ("…" if len(cid) > 16 else ""))
    for key in _DETAIL_KEYS:
        if key not in event or event[key] in (None, ""):
            continue
        value = event[key]
        if key in {"bid", "ask", "price", "price_limit", "from_tick", "to_tick", "net"}:
            rendered = _fmt_num(value, 3)
        elif key in {"size", "filled", "spent"}:
            rendered = _fmt_num(value, 4)
        else:
            rendered = str(value).replace("\n", " ")[:160]
        if rendered:
            details.append(f"{key}={rendered}")
    extra = "  ".join(details)
    return f"{ts}  {name:<22}  {extra}".rstrip()


def summarize_tape(events: List[dict]) -> Dict[str, Any]:
    counts = Counter(str(row.get("event") or "") for row in events)
    return {
        "events": len(events),
        "counts": counts,
        "first_ts": events[0].get("ts") if events else None,
        "last_ts": events[-1].get("ts") if events else None,
    }


def _fmt_num(value: object, digits: int) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)
