"""Persistence for shadow sim state, ticks, and completed trades.

All paths are under sim_data/<data_tag>/. Never touches bot state files.
Disk-safe: ENOSPC disables non-essential writes; prune enforces size caps.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional, Tuple

from . import config as cfg
from .config import assert_path_safe, ensure_dirs

log = logging.getLogger("shadow")

_disk_full = False
_last_disk_warn = 0.0


def is_disk_full() -> bool:
    return _disk_full


def mark_disk_full(where: str = "") -> None:
    global _disk_full, _last_disk_warn
    _disk_full = True
    now = time.time()
    if now - _last_disk_warn > 60:
        log.error(
            "DISK FULL (ENOSPC)%s - ticks/trades disabled until prune frees space",
            f" at {where}" if where else "",
        )
        _last_disk_warn = now


def clear_disk_full_if_space(sim: Optional[dict] = None) -> bool:
    global _disk_full
    if not _disk_full:
        return False
    free_mb, _ = disk_usage_mb(cfg.DATA_DIR)
    min_free = float((sim or {}).get("min_free_disk_mb", 200.0))
    if free_mb >= min_free:
        _disk_full = False
        log.warning("disk space recovered (%.0f MB free) - re-enabling optional writes", free_mb)
        return True
    return False


def _is_enospc(exc: BaseException) -> bool:
    if isinstance(exc, OSError):
        if getattr(exc, "errno", None) in (28,):
            return True
        if getattr(exc, "winerror", None) == 112:
            return True
    return "No space left" in str(exc)


def disk_usage_mb(path: str) -> Tuple[float, float]:
    try:
        ensure_dirs()
        if hasattr(os, "statvfs"):
            st = os.statvfs(path)
            free = (st.f_bavail * st.f_frsize) / (1024 * 1024)
            total = (st.f_blocks * st.f_frsize) / (1024 * 1024)
            return free, total
    except Exception:
        pass
    try:
        import shutil
        usage = shutil.disk_usage(path if os.path.exists(path) else cfg.DATA_ROOT)
        return usage.free / (1024 * 1024), usage.total / (1024 * 1024)
    except Exception:
        return 9999.0, 9999.0


def dir_size_bytes(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def load_state() -> dict:
    ensure_dirs()
    if not os.path.exists(cfg.STATE_FILE):
        return {"positions": {}, "completed": []}
    try:
        with open(cfg.STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("load_state failed: %s", e)
        return {"positions": {}, "completed": []}


def save_state(state: dict) -> None:
    ensure_dirs()
    assert_path_safe(cfg.STATE_FILE)
    tmp = cfg.STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, cfg.STATE_FILE)
    except OSError as e:
        if _is_enospc(e):
            mark_disk_full("save_state")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return
        raise


def append_jsonl(path: str, row: dict) -> None:
    ensure_dirs()
    assert_path_safe(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError as e:
        if _is_enospc(e):
            mark_disk_full(path)
            return
        raise


def append_result(row: dict) -> None:
    append_jsonl(cfg.RESULTS_FILE, row)


def append_tick(condition_id: str, row: dict) -> None:
    if _disk_full:
        return
    safe = "".join(c for c in condition_id if c.isalnum() or c in ("-", "_", "x"))
    if not safe:
        return
    path = os.path.join(cfg.TICKS_DIR, f"{safe}.jsonl")
    append_jsonl(path, row)


def save_trade(condition_id: str, trade: dict) -> None:
    if _disk_full:
        return
    ensure_dirs()
    safe = "".join(c for c in condition_id if c.isalnum() or c in ("-", "_", "x"))
    if not safe:
        return
    path = os.path.join(cfg.TRADES_DIR, f"{safe}.json")
    assert_path_safe(path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(trade, f, indent=2, default=str)
    except OSError as e:
        if _is_enospc(e):
            mark_disk_full(path)
            return
        raise


def _remove_oldest_files(directory: str, count: int) -> int:
    if count <= 0 or not os.path.isdir(directory):
        return 0
    files = []
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            try:
                files.append((os.path.getmtime(path), path))
            except OSError:
                pass
    files.sort()
    removed = 0
    for _mtime, path in files[:count]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def prune_old_files(sim: dict) -> dict:
    ensure_dirs()
    now = time.time()
    tick_max_age = float(sim.get("prune_ticks_after_hours", 6)) * 3600
    trade_max_age = float(sim.get("prune_trades_after_days", 7)) * 86400
    removed_ticks = 0
    removed_trades = 0
    freed_for_cap = 0

    if os.path.isdir(cfg.TICKS_DIR):
        for name in os.listdir(cfg.TICKS_DIR):
            path = os.path.join(cfg.TICKS_DIR, name)
            if not os.path.isfile(path):
                continue
            try:
                if now - os.path.getmtime(path) > tick_max_age:
                    os.remove(path)
                    removed_ticks += 1
            except OSError:
                pass

    if os.path.isdir(cfg.TRADES_DIR):
        for name in os.listdir(cfg.TRADES_DIR):
            path = os.path.join(cfg.TRADES_DIR, name)
            if not os.path.isfile(path):
                continue
            try:
                if now - os.path.getmtime(path) > trade_max_age:
                    os.remove(path)
                    removed_trades += 1
            except OSError:
                pass

    max_mb = float(sim.get("max_sim_data_mb", 150.0))
    size_mb = dir_size_bytes(cfg.DATA_ROOT) / (1024 * 1024)
    while size_mb > max_mb:
        n = 0
        for root, _dirs, files in os.walk(cfg.DATA_ROOT):
            if os.path.basename(root) != "ticks":
                continue
            batch = []
            for name in files:
                fp = os.path.join(root, name)
                try:
                    batch.append((os.path.getmtime(fp), fp))
                except OSError:
                    pass
            batch.sort()
            for _m, fp in batch[:25]:
                try:
                    os.remove(fp)
                    n += 1
                except OSError:
                    pass
        if n == 0:
            for root, _dirs, files in os.walk(cfg.DATA_ROOT):
                if os.path.basename(root) != "trades":
                    continue
                batch = []
                for name in files:
                    fp = os.path.join(root, name)
                    try:
                        batch.append((os.path.getmtime(fp), fp))
                    except OSError:
                        pass
                batch.sort()
                for _m, fp in batch[:10]:
                    try:
                        os.remove(fp)
                        n += 1
                    except OSError:
                        pass
        if n == 0:
            break
        freed_for_cap += n
        size_mb = dir_size_bytes(cfg.DATA_ROOT) / (1024 * 1024)

    free_mb, total_mb = disk_usage_mb(cfg.DATA_DIR)
    min_free = float(sim.get("min_free_disk_mb", 200.0))
    if free_mb < min_free:
        wiped = 0
        for root, _dirs, files in os.walk(cfg.DATA_ROOT):
            if os.path.basename(root) != "ticks":
                continue
            for name in files:
                try:
                    os.remove(os.path.join(root, name))
                    wiped += 1
                except OSError:
                    pass
        removed_ticks += wiped
        free_mb, total_mb = disk_usage_mb(cfg.DATA_DIR)
        if free_mb < min_free:
            mark_disk_full("prune_emergency")
        else:
            clear_disk_full_if_space(sim)
    else:
        clear_disk_full_if_space(sim)

    return {
        "removed_ticks": removed_ticks,
        "removed_trades": removed_trades,
        "freed_for_cap": freed_for_cap,
        "sim_data_mb": round(dir_size_bytes(cfg.DATA_ROOT) / (1024 * 1024), 1),
        "disk_free_mb": round(free_mb, 1),
        "disk_total_mb": round(total_mb, 1),
        "disk_full_flag": _disk_full,
    }


def summarize_results(path: Optional[str] = None) -> dict:
    path = path or cfg.RESULTS_FILE
    if not os.path.exists(path):
        return {"n": 0, "path": path}
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if not rows:
        return {"n": 0, "path": path}

    resolved = [r for r in rows if r.get("winner") is not None or r.get("resolved") is True]
    unresolved = [r for r in rows if r not in resolved]
    use = resolved if resolved else rows
    pnls = [float(r.get("pnl", 0)) for r in use]
    sells = [r for r in use if float(r.get("sell_filled", 0) or 0) > 0]
    misses = [
        r for r in use
        if r.get("triggered") and float(r.get("sell_filled", 0) or 0) <= 0
    ]
    return {
        "n": len(rows),
        "n_resolved": len(resolved),
        "n_unresolved": len(unresolved),
        "path": path,
        "mean_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
        "total_pnl": sum(pnls),
        "win_rate": (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else 0.0,
        "sell_rate": (len(sells) / len(use)) if use else 0.0,
        "trigger_miss_rate": (len(misses) / len(use)) if use else 0.0,
        "avg_sell_px": (
            sum(float(r.get("sell_avg_px", 0) or 0) for r in sells) / len(sells)
            if sells else None
        ),
        "avg_set_cost": (
            sum(float(r.get("set_cost", 0) or 0) for r in use) / len(use) if use else None
        ),
        "stats_on": "resolved" if resolved else "all",
    }
