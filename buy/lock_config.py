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
    "strategy1_enabled": True,
    "strategy2_enabled": True,
    # Each strategy keeps its own budget. The combined cap is the sum, so
    # strategy 2 cannot spend strategy 1's $20 in the same BTC 5m market.
    "per_market_usd": 40.0,
    "combined_per_market_usd": 40.0,
    "strategy1_market_usd": 20.0,
    "strategy2_market_usd": 20.0,
    "clip_usd": 5.0,
    "max_open_exposure_usd": 60.0,
    "daily_loss_stop_usd": 60.0,
    "min_cash_buffer_usd": 5.0,
    "dry_run_cash_usd": 500.0,
    # Strategy 1 (NIULAI4): once a second from tau 58 down to tau 1.
    "s1_tau_max": 58.0,
    "s1_tau_min": 1.0,
    "s1_clip_cooldown_s": 1.0,
    # Strategy 2 (R2e): Binance 3s move on BTC 5m, tau 5..300.
    "s2_tau_max": 300.0,
    "s2_tau_min": 5.0,
    "s2_move_s": 3.0,
    "s2_move_sigma": 2.0,
    "s2_ask_max": 0.98,
    "s2_q_edge_min": None,
    "s2_clip_cooldown_s": 1.0,
    "s2_sigma_window_s": 300.0,
    "s2_sigma_min_samples": 60,
    "strategy2_markets": ["btc_5m"],
    "ask_min": 0.02,
    "taker_fee_rate": 0.07,
    "taker_fee_exponent": 1.0,
    "max_pay": 0.97,
    "limit_tick_improve": 0,
    "price_tick": 0.01,
    "min_shares": 1.0,
    "min_order_usd": 1.0,
    "stale_price_s": 2.0,
    "stale_book_s": 2.0,
    "stale_binance_s": 2.0,
    "dry_run_latency_s": 0.20,
    "fast_poll_s": 0.05,
    # Pair a watched fill with our nearest same-outcome signal inside this
    # many seconds. Farther signals stay unpaired.
    "h2h_window_s": 10.0,
    # Head-to-head wallet tape. Off unless set: the RTDS activity socket
    # stays closed and is not an input to either strategy.
    "h2h_enabled": False,
    "market_rules": {
        "btc_15m": {"Z": 0.0, "Pmax": 0.97, "edge_min": 0.0},
        "btc_5m": {"Z": 0.25, "Pmax": 0.90, "edge_min": 0.0},
    },
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
    "eval_log_s": 30.0,
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
        # Strategy 1 trades both BTC books. Alts are break-even after fees.
        "btc_15m": True,
        "btc_5m": True,
        "eth_15m": False,
        "sol_15m": False,
        "xrp_15m": False,
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
    rules = {name: dict(row) for name, row in DEFAULTS["market_rules"].items()}
    for key, value in raw.items():
        if key == "markets" and isinstance(value, dict):
            for name, flag in value.items():
                markets[str(name)] = _bool(flag, False)
            continue
        if key == "market_rules" and isinstance(value, dict):
            for name, row in value.items():
                base = dict(rules.get(str(name)) or {})
                if isinstance(row, dict):
                    base.update(row)
                rules[str(name)] = base
            continue
        cfg[key] = value
    cfg["markets"] = markets
    cfg["market_rules"] = rules
    cfg["h2h_enabled"] = _bool(cfg.get("h2h_enabled"), False)
    if "combined_per_market_usd" not in raw and "per_market_usd" in raw:
        cfg["combined_per_market_usd"] = cfg["per_market_usd"]
    return cfg


def validate_config(cfg: Any) -> None:
    """Raise ``ValueError`` on a knob that would make entries unsafe."""
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    positive = (
        "per_market_usd",
        "combined_per_market_usd",
        "strategy1_market_usd",
        "strategy2_market_usd",
        "clip_usd",
        "max_open_exposure_usd",
        "s1_tau_max",
        "s2_tau_max",
        "s2_move_sigma",
        "stale_price_s",
        "stale_book_s",
        "stale_binance_s",
        "poll_s",
        "fast_poll_s",
        "price_tick",
        "settle_window_s",
        "max_pay",
        "dry_run_latency_s",
        "h2h_window_s",
    )
    for key in positive:
        if key in cfg and _num(cfg.get(key), -1.0) <= 0:
            raise ValueError(f"{key} must be > 0")
    if _num(cfg.get("max_pay"), 0) >= 1:
        raise ValueError("max_pay must be < 1")
    if _num(cfg.get("s2_ask_max"), 0) >= 1:
        raise ValueError("s2_ask_max must be < 1")
    if _num(cfg.get("ask_min"), 1) < 0 or _num(cfg.get("ask_min"), 0) >= 1:
        raise ValueError("ask_min must be in [0, 1)")
    q_edge = cfg.get("s2_q_edge_min")
    if q_edge is not None and q_edge != "":
        if not math.isfinite(_num(q_edge, float("nan"))):
            raise ValueError("s2_q_edge_min must be a number or null")
    if _num(cfg.get("daily_loss_stop_usd"), -1) < 0:
        raise ValueError("daily_loss_stop_usd must be >= 0")
    if _num(cfg.get("min_cash_buffer_usd"), -1) < 0:
        raise ValueError("min_cash_buffer_usd must be >= 0")
    markets = cfg.get("markets")
    if markets is not None and not isinstance(markets, dict):
        raise ValueError("markets must be an object")
    rules = cfg.get("market_rules")
    if rules is not None and not isinstance(rules, dict):
        raise ValueError("market_rules must be an object")
    for name, row in (rules or {}).items():
        if isinstance(row, dict) and row.get("combined_usd") is not None:
            if _num(row.get("combined_usd"), -1.0) <= 0:
                raise ValueError(f"market_rules.{name}.combined_usd must be > 0")
