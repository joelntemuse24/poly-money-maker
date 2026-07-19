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
import re
from copy import deepcopy

# ---- Market discovery / APIs (public, no auth) ----
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
# Default series; override via strategy.sim.json -> sim.series_slug
DEFAULT_SERIES_SLUG = "btc-up-or-down-15m"

# ---- Paths (ALL under sim_data/ — never bot state files) ----
SIM_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SIM_ROOT)
DATA_ROOT = os.path.join(REPO_ROOT, "sim_data")
# Active run paths are resolved after load_sim() from series_slug / data_tag
DATA_DIR = DATA_ROOT
TICKS_DIR = os.path.join(DATA_DIR, "ticks")
TRADES_DIR = os.path.join(DATA_DIR, "trades")
STATE_FILE = os.path.join(DATA_DIR, "shadow_state.json")
RESULTS_FILE = os.path.join(DATA_DIR, "results.jsonl")
LOG_FILE = os.path.join(DATA_DIR, "shadow.log")
LOCK_FILE = os.path.join(DATA_DIR, "shadow.lock")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "shadow.heartbeat")

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

# Strategy defaults ? 15m experiment (not live bot)
STRATEGY_DEFAULTS = {
    # Anytime 8c experiment: sell as soon as a leg bids <= 8c
    "sell_threshold": 0.08,
    "hedge_enabled": False,
    "hedge_threshold": 0.50,
    # Full market duration (covers hourly); sell not gated to last N minutes
    "sell_window_min": 120.0,
    "sell_grace_s": 5,
    "sell_cooldown_s": 3,
    # Last-chance off for this experiment (threshold-only anytime)
    "sell_lastchance_threshold": 0.35,
    "sell_lastchance_s": 0,
    # Require opposite bid >= this to fire threshold sell (0 = off)
    "sell_confirm_opposite": 0.70,
}

SIM_DEFAULTS = {
    "series_slug": DEFAULT_SERIES_SLUG,
    # Comma-separated multi-series (15m + hourly). Empty = use series_slug only.
    "series_slugs": "btc-up-or-down-15m,btc-up-or-down-hourly",
    # Fresh tag so anytime-8c results do not mix with prior 12c/2min run
    "data_tag": "15m1h-8c-conf",
    "set_cost": 1.043,
    "shares": 5.0,
    "use_live_entry_books": False,
    "max_set_cost": 0.99,
    "entry_limit_price": 0.99,
    "entry_fill_model": "depth",
    "entry_slippage": 0.0,
    "entry_retry_s": 15.0,
    "max_entry_attempts": 500,
    # Enter early enough that "anytime" has room (hourly up to ~55m left)
    "enter_max_ttm_min": 55.0,
    "enter_min_ttm_min": 2.5,
    "poll_far_s": 5.0,
    "poll_near_s": 1.0,
    "poll_sell_s": 0.5,
    "discover_horizon_min": 90.0,
    "discover_refresh_s": 25.0,
    "book_workers": 6,
    # Must cover full sell window — anytime means poll books for open positions
    "book_horizon_min": 120.0,
    "fill_model": "depth",
    "fill_slippage": 0.0,
    "no_fill_after_s": 0.0,
    "post_expiry_record_s": 90.0,
    "resolve_bid_edge": 0.70,
    # Ticks off by default — results.jsonl is enough for strategy stats
    "record_ticks": False,
    "tick_sample_far_s": 30.0,
    "tick_sample_near_s": 10.0,
    "tick_sample_sell_s": 5.0,
    "prune_ticks_after_hours": 6.0,
    "prune_trades_after_days": 7.0,
    "max_completed_ids": 500,
    "max_events_per_pos": 50,
    "prune_every_s": 120.0,
    # Disk guards (MB)
    "max_sim_data_mb": 150.0,
    "min_free_disk_mb": 200.0,
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


def _safe_tag(tag: str) -> str:
    tag = (tag or "default").strip().lower()
    tag = re.sub(r"[^a-z0-9._-]+", "-", tag)
    return tag or "default"


def apply_data_paths(sim: dict) -> dict:
    """Point all store paths at sim_data/<data_tag>/ for this run."""
    global DATA_DIR, TICKS_DIR, TRADES_DIR, STATE_FILE, RESULTS_FILE
    global LOG_FILE, LOCK_FILE, HEARTBEAT_FILE

    tag = _safe_tag(str(sim.get("data_tag") or "default"))
    DATA_DIR = os.path.join(DATA_ROOT, tag)
    TICKS_DIR = os.path.join(DATA_DIR, "ticks")
    TRADES_DIR = os.path.join(DATA_DIR, "trades")
    STATE_FILE = os.path.join(DATA_DIR, "shadow_state.json")
    RESULTS_FILE = os.path.join(DATA_DIR, "results.jsonl")
    LOG_FILE = os.path.join(DATA_DIR, "shadow.log")
    LOCK_FILE = os.path.join(DATA_DIR, "shadow.lock")
    HEARTBEAT_FILE = os.path.join(DATA_DIR, "shadow.heartbeat")
    sim = dict(sim)
    sim["data_tag"] = tag
    sim["data_dir"] = DATA_DIR
    return sim


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
    return apply_data_paths(cfg)


def series_slug_list(sim: dict) -> list:
    """Return list of Gamma series slugs from sim config."""
    raw = sim.get("series_slugs")
    if raw is None or raw == "" or raw == []:
        return [str(sim.get("series_slug") or DEFAULT_SERIES_SLUG)]
    if isinstance(raw, (list, tuple)):
        out = [str(s).strip() for s in raw if str(s).strip()]
        return out or [str(sim.get("series_slug") or DEFAULT_SERIES_SLUG)]
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    return parts or [str(sim.get("series_slug") or DEFAULT_SERIES_SLUG)]


def ensure_dirs() -> None:

    for d in (DATA_ROOT, DATA_DIR, TICKS_DIR, TRADES_DIR):
        os.makedirs(d, exist_ok=True)


def assert_path_safe(path: str) -> None:
    base = os.path.basename(path)
    if base in BOT_FORBIDDEN:
        raise RuntimeError(f"shadow sim refused to touch bot file: {path}")
    abs_p = os.path.abspath(path)
    ok_roots = (os.path.abspath(DATA_ROOT), os.path.abspath(SIM_ROOT))
    if not any(abs_p == r or abs_p.startswith(r + os.sep) for r in ok_roots):
        raise RuntimeError(f"shadow sim path outside allowed roots: {path}")
