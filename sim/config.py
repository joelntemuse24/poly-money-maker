"""Simulator + strategy configuration (independent of bot.py)."""

from __future__ import annotations

import json
import os
from copy import deepcopy

# ---- Market discovery / APIs (public, no auth) ----
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
SERIES_SLUG = "btc-up-or-down-5m"

# ---- Paths ----
SIM_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SIM_ROOT)
DATA_DIR = os.path.join(REPO_ROOT, "sim_data")
TICKS_DIR = os.path.join(DATA_DIR, "ticks")
TRADES_DIR = os.path.join(DATA_DIR, "trades")
STATE_FILE = os.path.join(DATA_DIR, "shadow_state.json")
RESULTS_FILE = os.path.join(DATA_DIR, "results.jsonl")
LOG_FILE = os.path.join(DATA_DIR, "shadow.log")

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
    # Entry: calibrated from Polymarket-History (~$1.045 complete set)
    "set_cost": 1.045,
    "shares": 5.0,
    # Enter paper position when market first seen with this much time left (minutes)
    "enter_max_ttm_min": 4.5,
    "enter_min_ttm_min": 0.8,  # don't enter if already inside/near sell window
    # Polling (seconds)
    "poll_far_s": 5.0,  # TTM > 2 min
    "poll_near_s": 1.0,  # 2 min >= TTM > sell window
    "poll_sell_s": 0.25,  # inside sell window
    # How far ahead to track markets (minutes)
    "discover_horizon_min": 35.0,
    "discover_refresh_s": 20.0,
    # Fill model
    # "depth" = walk live bid book (most realistic for FAK)
    # "best_bid" = fill fully at best bid if any size (optimistic)
    # "best_bid_partial" = fill min(our_size, best_size) at best bid only
    "fill_model": "depth",
    # Extra adverse slippage on top of walked book (absolute price, e.g. 0.01 = 1¢)
    "fill_slippage": 0.0,
    # If True, refuse fills when seconds_left <= this (models late FAK death)
    "no_fill_after_s": 0.0,
    # Keep recording books this many seconds after expiry for resolution
    "post_expiry_record_s": 90.0,
    # Resolve winner when one mid/bid dominates after expiry
    "resolve_bid_edge": 0.70,
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
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        strat = raw.get("strategy", raw)
        cfg = _coerce(STRATEGY_DEFAULTS, strat)
    return cfg


def load_sim(path: str | None = None) -> dict:
    path = path or STRATEGY_FILE
    cfg = deepcopy(SIM_DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        sim = raw.get("sim", raw if "set_cost" in raw else {})
        cfg = _coerce(SIM_DEFAULTS, sim)
    return cfg


def ensure_dirs() -> None:
    for d in (DATA_DIR, TICKS_DIR, TRADES_DIR):
        os.makedirs(d, exist_ok=True)
