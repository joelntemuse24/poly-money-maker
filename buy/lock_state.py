"""Paper and live lockbot ledgers. The two files never share P&L."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


PAPER_NAME = "positions_lockbot.json"
LIVE_NAME = "positions_lockbot_live.json"
WINDOW_NAME = "lockbot_windows.json"
# A restart during the window, or just after it, still needs the open print.
WINDOW_KEEP_S = 6 * 3600.0


def ledger_name(dry_run: bool) -> str:
    """Paper keeps the historical filename. Live is a different file."""
    return PAPER_NAME if dry_run else LIVE_NAME


def ledger_path(root: Path, dry_run: bool) -> Path:
    return Path(root) / ledger_name(dry_run)


def empty_ledger(kind: str, *, now: Optional[float] = None) -> dict:
    payload = {"positions": {}, "intents": {}, "redeems": {}, "ledger": kind}
    if now is not None:
        payload["reset_ts"] = float(now)
    return payload


def load_ledger(path: Path, kind: str) -> dict:
    """Read one ledger. A missing or broken file is an empty book of ``kind``."""
    if not path.exists():
        return empty_ledger(kind)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_ledger(kind)
    if not isinstance(payload, dict):
        return empty_ledger(kind)
    payload.setdefault("positions", {})
    payload.setdefault("intents", {})
    payload.setdefault("redeems", {})
    payload["ledger"] = kind
    return payload


def save_ledger(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def reset_live_ledger(root: Path, *, now: Optional[float] = None) -> Path:
    """Zero the live book. The paper file is not opened."""
    path = ledger_path(root, False)
    save_ledger(path, empty_ledger("live", now=time.time() if now is None else now))
    return path


def window_file(root: Path) -> Path:
    return Path(root) / WINDOW_NAME


def _strike_value(row: dict) -> Optional[float]:
    try:
        strike = float(row.get("strike"))
    except (TypeError, ValueError):
        return None
    if not strike or strike <= 0:
        return None
    return strike


def prune_windows(rows: dict, now: float, *, keep_s: float = WINDOW_KEEP_S) -> dict:
    """Drop windows that ended long enough ago that settlement is done."""
    kept: dict = {}
    spot = float(now)
    horizon = float(keep_s)
    for slug, row in (rows or {}).items():
        if not isinstance(row, dict) or _strike_value(row) is None:
            continue
        end = row.get("end_ts")
        try:
            end_f = float(end) if end is not None else None
        except (TypeError, ValueError):
            end_f = None
        if end_f is not None:
            if spot > end_f + horizon:
                continue
        else:
            try:
                latched = float(row.get("latched_at") or 0.0)
            except (TypeError, ValueError):
                latched = 0.0
            if spot > latched + horizon:
                continue
        kept[str(slug)] = dict(row)
    return kept


def load_windows(path: Path, now: float) -> dict:
    """Slug -> window row. A missing or broken file is an empty map."""
    if not Path(path).exists():
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = payload.get("windows") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        return {}
    return prune_windows(rows, now)


def save_windows(path: Path, rows: dict, now: float) -> dict:
    """Write the pruned map. Returns what was written."""
    kept = prune_windows(rows, now)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    payload = {"windows": kept}
    temporary.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)
    return kept


def strike_for_position(pos: dict, latched: dict) -> Optional[float]:
    """Position strike, or the latched window strike when the fill had none."""
    if isinstance(pos, dict) and pos.get("strike") is not None:
        try:
            strike = float(pos["strike"])
        except (TypeError, ValueError):
            strike = None
        if strike is not None and strike > 0:
            return strike
    slug = str((pos or {}).get("slug") or "")
    remembered = (latched or {}).get(slug)
    try:
        strike = float(remembered)
    except (TypeError, ValueError):
        return None
    if strike <= 0:
        return None
    return strike


def decision_wait_s(
    now: float,
    *,
    poll_s: float,
    s1_next: Optional[float] = None,
    paper_next: Optional[float] = None,
    window_in: Optional[float] = None,
) -> float:
    """Seconds the decision thread may block. A trade event wakes it sooner.

    ``fast_poll_s`` is not a spin interval. The wait is the next strategy-1
    second, the next paper fill, the next window, or ``poll_s``, whichever
    comes first.
    """
    wait = max(0.0, float(poll_s))
    for stamp in (s1_next, paper_next):
        if stamp is None:
            continue
        wait = min(wait, max(0.0, float(stamp) - float(now)))
    if window_in is not None:
        wait = min(wait, max(0.0, float(window_in)))
    return wait
