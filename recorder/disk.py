"""Keep the recorder from filling the disk mintbot writes state to.

Checked at startup and about once a minute. Durable tape is removed
once its age in days is no longer below ``DURABLE_TAPE_KEEP_DAYS``.
Crossing ``TAPE_MAX_BYTES`` does not delete anything here: the upload
job copies those days to Drive and deletes them only after the hash
matches. Free space under ``DISK_FREE_MIN`` still deletes the oldest
tape day (durable copy, then that day's spool) so mintbot can write.
A day with an open writer handle is left in place. If free space is
still under the floor, new tape rows are dropped. Mintbot logs,
journals, and ``logs/archive`` are not touched.
"""

from __future__ import annotations

import os
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional


DISK_FREE_MIN = 4 * 1024 ** 3
TAPE_MAX_BYTES = 5 * 1024 ** 3
DURABLE_TAPE_KEEP_DAYS = 7


def _parse_day(name: str) -> Optional[date]:
    try:
        return datetime.strptime(name, "%Y-%m-%d").date()
    except ValueError:
        return None


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def tape_bytes(spool: Path, durable_root: Path) -> int:
    """Bytes in ``data/tape`` plus ``logs/durable/*/tape``."""
    total = dir_size(spool)
    if durable_root.is_dir():
        for child in durable_root.iterdir():
            tape = child / "tape"
            if tape.is_dir():
                total += dir_size(tape)
    return total


def _free_space(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return int(shutil.disk_usage(probe).free)


def tape_days(spool: Path, durable_root: Path) -> list[str]:
    found: set[str] = set()
    if spool.is_dir():
        for child in spool.iterdir():
            if child.is_dir() and _parse_day(child.name) is not None:
                found.add(child.name)
    if durable_root.is_dir():
        for child in durable_root.iterdir():
            if child.is_dir() and _parse_day(child.name) is not None and (child / "tape").is_dir():
                found.add(child.name)
    return sorted(found)


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)


def enforce_tape_cap(
    spool: Path,
    durable_root: Path,
    *,
    free_min: int = DISK_FREE_MIN,
    tape_max: int = TAPE_MAX_BYTES,
    keep_days: int = DURABLE_TAPE_KEEP_DAYS,
    today: Optional[date] = None,
    protected_days: Optional[set[str]] = None,
    free_bytes_fn: Optional[Callable[[Path], int]] = None,
    writer: object = None,
) -> dict:
    """Apply retention and the hard cap. Returns what was removed."""
    day = today or datetime.now(timezone.utc).date()
    limit = max(0, int(keep_days))
    free_of = free_bytes_fn or _free_space
    deleted: list[dict] = []

    def protected() -> set[str]:
        days = set(protected_days or ())
        opener = getattr(writer, "open_days", None)
        if opener is not None:
            days |= set(opener())
        return days

    if durable_root.is_dir():
        for child in sorted(durable_root.iterdir(), key=lambda item: item.name):
            parsed = _parse_day(child.name)
            if parsed is None or (day - parsed).days < limit:
                continue
            if child.name in protected():
                continue
            tape = child / "tape"
            if not tape.is_dir():
                continue
            try:
                _remove_tree(tape)
            except OSError:
                deleted.append({"day": child.name, "where": "durable", "why": "retention", "error": "unlink"})
                continue
            deleted.append({"day": child.name, "where": "durable", "why": "retention"})

    def measure() -> tuple[int, int]:
        return int(free_of(spool)), tape_bytes(spool, durable_root)

    free, used = measure()
    if free < int(free_min):
        for name in tape_days(spool, durable_root):
            if name in protected():
                continue
            free, used = measure()
            if free >= int(free_min):
                break
            durable_tape = durable_root / name / "tape"
            if durable_tape.is_dir():
                try:
                    _remove_tree(durable_tape)
                    deleted.append({"day": name, "where": "durable", "why": "low_disk"})
                except OSError:
                    deleted.append({"day": name, "where": "durable", "why": "low_disk", "error": "unlink"})
            free, used = measure()
            if free >= int(free_min):
                break
            if name in protected():
                continue
            spool_day = spool / name
            if spool_day.is_dir():
                try:
                    _remove_tree(spool_day)
                    deleted.append({"day": name, "where": "spool", "why": "low_disk"})
                except OSError:
                    deleted.append({"day": name, "where": "spool", "why": "low_disk", "error": "unlink"})
    free, used = measure()
    low_disk = free < int(free_min)
    if writer is not None:
        setter = getattr(writer, "set_writes_enabled", None)
        if setter is not None:
            setter(not low_disk)
        else:
            setattr(writer, "writes_enabled", not low_disk)
    return {
        "deleted": deleted,
        "free_bytes": free,
        "tape_bytes": used,
        "low_disk": low_disk,
        "over_tape_cap": used > int(tape_max),
        "writes_enabled": not low_disk,
    }


def disk_alerts(result: dict) -> list[dict]:
    """Loud records for tape deleted because free disk was under the floor."""
    emergency = [row for row in result.get("deleted") or [] if row.get("why") == "low_disk"]
    if not emergency:
        return []
    return [
        {
            "event": "recorder_disk_emergency",
            "level": "error",
            "message": "free disk under the floor; deleted oldest tape without a verified upload",
            "deleted": emergency,
            "free_bytes": result.get("free_bytes"),
            "tape_bytes": result.get("tape_bytes"),
        }
    ]
