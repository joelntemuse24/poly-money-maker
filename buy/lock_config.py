"""Hot-reloadable settings for the idle settlement and book logging shell."""
from __future__ import annotations

import math
from typing import Any

DEFAULTS: dict[str, Any] = {'enabled': False,
 'dry_run': True,
 'book_log_enabled': False,
 'book_log_levels': 5,
 'book_log_min_interval_ms': 100,
 'book_log_path': 'logs/books.jsonl',
 'book_log_max_bytes': 104857600,
 'strike_tol_s': 0.75,
 'poll_s': 1.0,
 'gamma_refresh_s': 20.0,
 'market_refresh_s': 20.0,
 'history_s': 1200.0,
 'markets': {'btc_15m': True,
             'btc_5m': True,
             'eth_15m': False,
             'sol_15m': False,
             'xrp_15m': False,
             'eth_5m': False,
             'sol_5m': False,
             'xrp_5m': False},
 'gamma_url': 'https://gamma-api.polymarket.com'}


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def apply_defaults(raw: Any) -> dict:
    """Load supported shell settings; retired entry knobs are ignored."""
    cfg = dict(DEFAULTS)
    cfg["markets"] = dict(DEFAULTS["markets"])
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key == "markets" and isinstance(value, dict):
                cfg[key].update({str(name): _bool(flag, False) for name, flag in value.items()})
            elif key in DEFAULTS:
                cfg[key] = value
    for key in ("enabled", "dry_run", "book_log_enabled"):
        cfg[key] = _bool(cfg[key], DEFAULTS[key])
    return cfg


def validate_config(cfg: Any) -> None:
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    for key in ("poll_s", "history_s", "market_refresh_s", "gamma_refresh_s", "strike_tol_s", "book_log_levels", "book_log_min_interval_ms", "book_log_max_bytes"):
        try:
            value = float(cfg.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a finite number") from None
        minimum = 0 if key in {"book_log_min_interval_ms", "book_log_max_bytes"} else 1e-12
        if not math.isfinite(value) or value < minimum:
            raise ValueError(f"{key} is out of range")
        if key == "book_log_levels" and value != int(value):
            raise ValueError("book_log_levels must be a positive integer")
    path = cfg.get("book_log_path", DEFAULTS["book_log_path"])
    if not isinstance(path, str) or not path.strip():
        raise ValueError("book_log_path must be a non-empty string")
    if not isinstance(cfg.get("markets", {}), dict):
        raise ValueError("markets must be an object")
