from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from typing import Iterator

from .config import DATA_DIR, HEARTBEAT_FILE, LOCK_FILE, STATE_FILE


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def free_disk_mb() -> float:
    ensure_data_dir()
    return shutil.disk_usage(DATA_DIR).free / (1024 * 1024)


def load_state() -> dict:
    ensure_data_dir()
    if not os.path.exists(STATE_FILE):
        return {"intents": {}, "dry_plans": []}
    with open(STATE_FILE, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("buyer state must be an object")
    payload.setdefault("intents", {})
    payload.setdefault("dry_plans", [])
    return payload


def save_state(state: dict) -> None:
    ensure_data_dir()
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, STATE_FILE)


def write_heartbeat(status: str, **fields) -> None:
    ensure_data_dir()
    payload = {"ts": time.time(), "status": status}
    payload.update(fields)
    temporary = HEARTBEAT_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
    os.replace(temporary, HEARTBEAT_FILE)


def trim_state(state: dict, max_intents: int, max_dry_plans: int) -> None:
    intents = state.setdefault("intents", {})
    if len(intents) > max_intents:
        ordered = sorted(
            intents,
            key=lambda condition_id: float(intents[condition_id].get("created_at") or 0),
        )
        removable = [
            condition_id
            for condition_id in ordered
            if intents[condition_id].get("status") in ("completed", "failed", "invalid")
        ]
        for condition_id in removable[: max(0, len(intents) - max_intents)]:
            intents.pop(condition_id, None)
    plans = state.setdefault("dry_plans", [])
    if len(plans) > max_dry_plans:
        state["dry_plans"] = plans[-max_dry_plans:]


@contextmanager
def single_instance_lock() -> Iterator[None]:
    ensure_data_dir()
    handle = open(LOCK_FILE, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == "":
                handle.seek(0)
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
