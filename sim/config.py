"""Simulator + strategy configuration (independent of bot.py).

Isolation guarantees:
  - Never imports bot.py
  - Never reads/writes positions.json, strategy.json, .env, bot.log, pnl.json
  - All runtime I/O under sim_data/ only
  - Config file is sim/strategy.sim.json (not the live bot's strategy.json)
  - Public HTTP only (Gamma + CLOB book) — no API keys, no order endpoints
"""

from __future__ import annotations

import json
import os
from copy import deepcopy

# ---- Market discovery / APIs (public, no auth) ----
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
SERIES_SLUG = "btc-up-or-down-5m"

# ---- Paths (ALL under sim_data/ — never bot state files) ----
SIM_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SIM_ROOT)
DATA_DIR = os.path.join(REPO_ROOT, "sim_data")
TICKS_DIR = os.path.join(DATA_DIR, "ticks")
TRADES_DIR = os.path.join(DATA_DIR, "trades")
STATE_FILE = os.path.join(DATA_DIR, "shadow_state.json")
RESULTS_FILE = os.path.join(DATA_DIR, "results.jsonl")
LOG_FILE = os.path.join(DATA_DIR, "shadow.log")
LOCK_FILE = os.path.join(DATA_DIR, "shadow.lock")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "shadow.heartbeat")

# Forbidden paths we must never touch (defense in depth)
BOT_FORBIDDEN = frozenset(
    {
        "positions.json",
        "strategy.json",
        "pnl.json",
        "bot.log",
        ".env",
        ".dashboard_status.json",
        ".heartbeat",
    }
)

# ---- Strategy defaults (mirror live bot at c8ea675) ----
STRATEGY_DEFAULTS = {
    "sell_threshold": 0.10,
    "hedge_enabled": False,
    "hedge_threshold": 0.50,
    "sell_window_min": 0.75,  # 45 seconds
    "sell_grace_s": 2,
    "sell_cooldown_s": 3,
    "sell_lastchance_threshold": 0.35,
    "sell_lastchance_s": 10,
}

# ---- Simulation economics / execution ----
SIM_DEFAULTS = {
    # Entry: calibrated from Polymarket-History (~$1.043 complete set)
    "set_cost": 1.043,
    "shares": 5.0,
    # Enter paper position when market first seen with this much time left (minutes)
    "enter_max_ttm_min": 4.5,
    "enter_min_ttm_min": 0.8,
    # Polling (seconds) — adaptive; keep mild so we don't starve polybot CPU/API
    "poll_far_s": 5.0,
    "poll_near_s": 1.0,
    "poll_sell_s": 0.35,  # slightly slower than bot 0.25s to reduce shared-API contention
    # How far ahead to track markets (minutes)
    "discover_horizon_min": 30.0,
    "discover_refresh_s": 25.0,  # cache discovery; don't hit Gamma every cycle
    # Book fetch concurrency (lower = friendlier to CLOB + bot)
    "book_workers": 6,
    # Only fetch full books for positions with TTM under this (minutes).
    "book_horizon_min": 3.0,
    # Fill model
    "fill_model": "depth",
    "fill_slippage": 0.0,
    "no_fill_after_s": 0.0,
    "post_expiry_record_s": 90.0,
    "resolve_bid_edge": 0.70,
    # Disk hygiene
    "tick_sample_far_s": 5.0,
    "tick_sample_near_s": 1.0,
    "tick_sample_sell_s": 0.35,
    "prune_ticks_after_hours": 48.0,
    "prune_trades_after_days": 14.0,
    "max_completed_ids": 500,
    "max_events_per_pos": 200,
    "prune_every_s": 300.0,
}

STRATEGY_FILE = os.path.join(SIM_ROOT, "strategy.sim.json")


def _coerce(defaults: dict, overrides: dict) -> dict:
    cfg = deepcopy(defaults)
    for k, v in (overrides or {}).items():
        if k not in cfg:
            continue
        expected = type(cfg[k])
        if expected is bool:
            cfg[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
        else:
            cfg[k] = expected(v)
    return cfg


def load_strategy(path: str | None = None) -> dict:
    path = path or STRATEGY_FILE
    cfg = deepcopy(STRATEGY_DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        strat = raw.get("strategy", raw)
        cfg = _coerce(STRATEGY_DEFAULTS, strat)
    return cfg


def load_sim(path: str | None = None) -> dict:
    path = path or STRATEGY_FILE
    cfg = deepcopy(SIM_DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        sim = raw.get("sim", raw if "set_cost" in raw else {})
        cfg = _coerce(SIM_DEFAULTS, sim)
    return cfg


def ensure_dirs() -> None:
    for d in (DATA_DIR, TICKS_DIR, TRADES_DIR):
        os.makedirs(d, exist_ok=True)


def assert_path_safe(path: str) -> None:
    """Raise if a write target would touch bot-owned files."""
    base = os.path.basename(path)
    if base in BOT_FORBIDDEN:
        raise RuntimeError(f"shadow sim refused to touch bot file: {path}")
    abs_p = os.path.abspath(path)
    ok_roots = (os.path.abspath(DATA_DIR), os.path.abspath(SIM_ROOT))
    if not any(abs_p == r or abs_p.startswith(r + os.sep) for r in ok_roots):
        raise RuntimeError(f"shadow sim path outside allowed roots: {path}")
