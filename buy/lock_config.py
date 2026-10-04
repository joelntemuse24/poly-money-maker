"""Hot-reloadable lockbot knobs. Every threshold lives here."""

from __future__ import annotations

import math
from typing import Any


# Repo defaults. ``lockbot.example.json`` mirrors these. ``dry_run`` stays
# true until the operator copies the file and turns it off. ``enabled``
# true means new entries are allowed; set it false to stop entries
# without stopping redeem or logging.
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "dry_run": True,
    "per_market_usd": 20.0,
    "max_open_exposure_usd": 100.0,
    "daily_loss_stop_usd": 60.0,
    "min_cash_buffer_usd": 5.0,
    "dry_run_cash_usd": 500.0,
    "entry_window_s": 60.0,
    "entry_min_ttm_s": 1.0,
    "p_min": 0.80,
    "ask_min": 0.30,
    "ask_max": 0.95,
    "edge_min": 0.03,
    "taker_fee_rate": 0.07,
    "taker_fee_exponent": 1.0,
    "max_pay": 0.97,
    "limit_tick_improve": 0,
    "price_tick": 0.01,
    "max_entries_per_market": 1,
    "min_shares": 5.0,
    "min_order_usd": 1.0,
    "stale_price_s": 2.0,
    "stale_book_s": 2.0,
    "elapsed_max_gap_s": 3.0,
    "strike_match_usd": 0.01,
    "strike_match_rel": 0.0001,
    "strike_tol_s": 0.75,
    "vol_short_s": 300,
    "vol_long_s": 900,
    "vol_min_samples": 60,
    "noise_frac": 0.00002,
    "settle_window_s": 60.0,
    "poll_s": 1.0,
    "eval_log_s": 5.0,
    "book_warm_s": 15.0,
    "gamma_refresh_s": 20.0,
    "market_refresh_s": 20.0,
    "history_s": 1200.0,
    "notify_entry_whatsapp": False,
    "redeem_enabled": True,
    "redeem_startup_sweep": False,
    "redeem_poll_s": 15.0,
    "redeem_min_after_end_s": 60.0,
    "redeem_retry_s": 60.0,
    "redeem_max_attempts": 6,
    "redeem_tx_timeout_s": 300.0,
    "redeem_min_payout_usd": 0.01,
    "position_tolerance": 0.01,
    "clob_url": "https://clob.polymarket.com",
    "gamma_url": "https://gamma-api.polymarket.com",
    "data_api_url": "https://data-api.polymarket.com",
    "relayer_url": "https://relayer-v2.polymarket.com",
    "rpc_url": "https://polygon.drpc.org",
    "chain_id": 137,
    "pUSD_address": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
    "ctf_address": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "standard_adapter_address": "0xAdA100Db00Ca00073811820692005400218FcE1f",
    "markets": {
        "btc_15m": True,
        "eth_15m": True,
        "sol_15m": True,
        "xrp_15m": True,
        "btc_5m": True,
        # Same Chainlink 60s TWAP, but not in the requested book.
        "eth_5m": False,
        "sol_5m": False,
        "xrp_5m": False,
    },
}


def _num(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def apply_defaults(raw: Any) -> dict:
    """Overlay a JSON object on ``DEFAULTS``. Nested ``markets`` merges."""
    cfg = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    markets = dict(DEFAULTS["markets"])
    for key, value in raw.items():
        if key == "markets" and isinstance(value, dict):
            for name, flag in value.items():
                markets[str(name)] = _bool(flag, False)
            continue
        cfg[key] = value
    cfg["markets"] = markets
    return cfg


def validate_config(cfg: Any) -> None:
    """Raise ``ValueError`` on a knob that would make entries unsafe."""
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    positive = (
        "per_market_usd",
        "max_open_exposure_usd",
        "entry_window_s",
        "stale_price_s",
        "stale_book_s",
        "poll_s",
        "price_tick",
        "settle_window_s",
        "max_pay",
    )
    for key in positive:
        if key in cfg and _num(cfg.get(key), -1.0) <= 0:
            raise ValueError(f"{key} must be > 0")
    if _num(cfg.get("p_min"), -1) <= 0 or _num(cfg.get("p_min"), 2) > 1:
        raise ValueError("p_min must be in (0, 1]")
    if _num(cfg.get("max_pay"), 0) >= 1:
        raise ValueError("max_pay must be < 1")
    if _num(cfg.get("ask_max"), 0) > _num(cfg.get("max_pay"), 0.97) + 1e-9:
        # ask_max above the sanity cap is allowed only when max_pay still
        # clips the order. Reject a cap that could never bind below 1.
        if _num(cfg.get("ask_max"), 0) >= 1:
            raise ValueError("ask_max must be < 1")
    try:
        if int(cfg.get("max_entries_per_market", 1)) < 1:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("max_entries_per_market must be >= 1")
    if _num(cfg.get("daily_loss_stop_usd"), -1) < 0:
        raise ValueError("daily_loss_stop_usd must be >= 0")
    if _num(cfg.get("min_cash_buffer_usd"), -1) < 0:
        raise ValueError("min_cash_buffer_usd must be >= 0")
    markets = cfg.get("markets")
    if markets is not None and not isinstance(markets, dict):
        raise ValueError("markets must be an object")
