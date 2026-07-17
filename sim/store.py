"""Persistence for shadow sim state, ticks, and completed trades.

All paths are under sim_data/<data_tag>/. Never touches bot state files.
"""

from __future__ import annotations

import json
import os
import time
from typing import List, Optional

from . import config as cfg
from .config import assert_path_safe, ensure_dirs


def load_state() -> dict:
    ensure_dirs()
    if not os.path.exists(cfg.STATE_FILE):
        return {"positions": {}, "completed": []}
    with open(cfg.STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    ensure_dirs()
    assert_path_safe(cfg.STATE_FILE)
    tmp = cfg.STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, cfg.STATE_FILE)


def append_jsonl(path: str, row: dict) -> None:
    ensure_dirs()
    assert_path_safe(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def append_result(row: dict) -> None:
    append_jsonl(cfg.RESULTS_FILE, row)


def append_tick(condition_id: str, row: dict) -> None:
    safe = "".join(c for c in condition_id if c.isalnum() or c in ("-", "_", "x"))
    if not safe:
        return
    path = os.path.join(cfg.TICKS_DIR, f"{safe}.jsonl")
    append_jsonl(path, row)


def save_trade(condition_id: str, trade: dict) -> None:
    ensure_dirs()
    safe = "".join(c for c in condition_id if c.isalnum() or c in ("-", "_", "x"))
    if not safe:
        return
    path = os.path.join(cfg.TRADES_DIR, f"{safe}.json")
    assert_path_safe(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trade, f, indent=2, default=str)


def prune_old_files(sim: dict) -> dict:
    ensure_dirs()
    now = time.time()
    tick_max_age = float(sim.get("prune_ticks_after_hours", 48)) * 3600
    trade_max_age = float(sim.get("prune_trades_after_days", 14)) * 86400
    removed_ticks = 0
    removed_trades = 0

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

    return {"removed_ticks": removed_ticks, "removed_trades": removed_trades}


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
    pnls = [float(r.get("pnl", 0)) for r in rows]
    sells = [r for r in rows if float(r.get("sell_filled", 0) or 0) > 0]
    misses = [
        r
        for r in rows
        if r.get("triggered") and float(r.get("sell_filled", 0) or 0) <= 0
    ]
    return {
        "n": len(rows),
        "path": path,
        "mean_pnl": sum(pnls) / len(pnls),
        "total_pnl": sum(pnls),
        "win_rate": sum(1 for p in pnls if p > 0) / len(pnls),
        "sell_rate": len(sells) / len(rows),
        "trigger_miss_rate": len(misses) / len(rows),
        "avg_sell_px": (
            sum(float(r.get("sell_avg_px", 0) or 0) for r in sells) / len(sells)
            if sells
            else None
        ),
        "avg_set_cost": sum(float(r.get("set_cost", 0) or 0) for r in rows) / len(rows),
    }
