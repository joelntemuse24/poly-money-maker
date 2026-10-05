"""Paper and live lockbot ledgers. The two files never share P&L."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


PAPER_NAME = "positions_lockbot.json"
LIVE_NAME = "positions_lockbot_live.json"


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
