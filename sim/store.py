"""Persistence for shadow sim state, ticks, and completed trades.

All paths are under sim_data/. Never touches bot state files.
"""

from __future__ import annotations

import json
import os
import time
from typing import List

from .config import (
    RESULTS_FILE,
    STATE_FILE,
    TICKS_DIR,
    TRADES_DIR,
    assert_path_safe,
    ensure_dirs,
)


def load_state() -> dict:
    ensure_dirs()
    if not os.path.exists(STATE_FILE):
        return {"positions": {}, "completed": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    ensure_dirs()
    assert_path_safe(STATE_FILE)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def append_jsonl(path: str, row: dict) -> None:
    ensure_dirs()
    assert_path_safe(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def append_result(row: dict) -> None:
    append_jsonl(RESULTS_FILE, row)


def append_tick(condition_id: str, row: dict) -> None:
    # sanitize id so path cannot escape ticks dir
    safe = "".join(c for c in condition_id if c.isalnum() or c in ("-", "_", "x"))
    if not safe:
        return
    path = os.path.join(TICKS_DIR, f"{safe}.jsonl")
    append_jsonl(path, row)


def save_trade(condition_id: str, trade: dict) -> None:
    ensure_dirs()
    safe = "".join(c for c in condition_id if c.isalnum() or c in ("-", "_", "x"))
    if not safe:
        return
    path = os.path.join(TRADES_DIR, f"{safe}.json")
    assert_path_safe(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trade, f, indent=2, default=str)


def prune_old_files(sim: dict) -> dict:
    """Delete old tick/trade files to protect disk on small VMs."""
    ensure_dirs()
    now = time.time()
    tick_max_age = float(sim.get("prune_ticks_after_hours", 48)) * 3600
    trade_max_age = float(sim.get("prune_trades_after_days", 14)) * 86400
    removed_ticks = 0
    removed_trades = 0

    for name in os.listdir(TICKS_DIR):
        path = os.path.join(TICKS_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            if now - os.path.getmtime(path) > tick_max_age:
                os.remove(path)
                removed_ticks += 1
        except OSError:
            pass

    for name in os.listdir(TRADES_DIR):
        path = os.path.join(TRADES_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            if now - os.path.getmtime(path) > trade_max_age:
                os.remove(path)
                removed_trades += 1
        except OSError:
            pass

    return {"removed_ticks": removed_ticks, "removed_trades": removed_trades}


def summarize_results(path: str = RESULTS_FILE) -> dict:
    if not os.path.exists(path):
        return {"n": 0}
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
        return {"n": 0}
    pnls = [float(r.get("pnl", 0)) for r in rows]
    sells = [r for r in rows if float(r.get("sell_filled", 0) or 0) > 0]
    misses = [
        r
        for r in rows
        if r.get("triggered") and float(r.get("sell_filled", 0) or 0) <= 0
    ]
    return {
        "n": len(rows),
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
