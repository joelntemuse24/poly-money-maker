"""NIULAI4 fast-follow strategy gate and detection helpers."""

from __future__ import annotations

import math
from typing import Any

from buy.lock_wallets import WALLETS, fill_key, is_btc_updown, window_kind


NIULAI4 = "0x44832d0d2ec11187c1e77d786feb15f6a50254c6"
S3_MARKETS = {"btc_5m", "btc_15m"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def s3_fill_key(fill: dict) -> str:
    """Stable fill identity for both RTDS and future chain/data-api sources."""
    return fill_key(fill)


def normalize_s3_fill(fill: dict) -> dict | None:
    """Keep only NIULAI4 BUYs in BTC 5m/15m markets."""
    if not isinstance(fill, dict):
        return None
    wallet = str(fill.get("wallet") or fill.get("proxyWallet") or "").lower()
    slug = str(fill.get("slug") or fill.get("eventSlug") or "")
    side = str(fill.get("trade_side") or fill.get("side") or "").upper()
    if wallet != NIULAI4 or side != "BUY" or not is_btc_updown(slug):
        return None
    duration = window_kind(slug)
    if duration not in {"5m", "15m"}:
        return None
    price = _num(fill.get("price"), -1.0)
    size = _num(fill.get("size"), -1.0)
    if price <= 0 or size <= 0:
        return None
    out = dict(fill)
    out.update(wallet=wallet, slug=slug, duration=duration, trade_side="BUY", price=price, size=size)
    out["fill_id"] = out.get("fill_id") or s3_fill_key(out)
    return out


def evaluate_strategy3(view: dict, account: dict, cfg: dict) -> dict:
    """Make one fixed-limit copy-follow decision from a new NIULAI4 fill."""
    view = view or {}
    account = account or {}
    cfg = cfg or {}
    fill = normalize_s3_fill(view.get("his_fill") or {})
    base = {
        "strategy": "s3",
        "action": "skip",
        "reason": "strategy_off",
        "slug": view.get("slug"),
        "market_key": view.get("market_key") or view.get("key"),
        "his_price": None if fill is None else fill["price"],
        "his_size": None if fill is None else fill["size"],
        "his_tx": None if fill is None else fill.get("tx") or fill.get("transactionHash"),
        "detect_source": view.get("detect_source"),
        "detect_lag_ms": view.get("detect_lag_ms"),
    }
    if not bool(cfg.get("strategy3_enabled", False)):
        return base
    if fill is None:
        base["reason"] = "fill_ignored"
        return base
    key = str(view.get("market_key") or view.get("key") or "")
    if key not in S3_MARKETS:
        base["reason"] = "strategy_market_off"
        return base
    ttm = _num(view.get("ttm_s"), -1.0)
    base.update(
        side=str(fill.get("outcome") or "").strip().lower(),
        token_id=fill.get("asset"),
        ttm_s=ttm,
        our_limit=_num(cfg.get("strategy3_limit"), 0.99),
        limit=_num(cfg.get("strategy3_limit"), 0.99),
    )
    if base["side"] not in {"up", "down"} or not base["token_id"]:
        base["reason"] = "token_unknown"
        return base
    if ttm < _num(cfg.get("strategy3_min_ttm_s"), 3.0):
        base["reason"] = "too_late"
        return base
    if str(fill.get("fill_id")) in set(view.get("seen_fill_ids") or ()):
        base["reason"] = "duplicate_fill"
        return base
    base["notional"] = _num(cfg.get("strategy3_clip_usd"), 10.0)
    base["shares"] = base["notional"] / base["limit"]
    if _num(account.get("spent_s3"), 0.0) + _num(account.get("pending_s3"), 0.0) + base["notional"] > _num(cfg.get("strategy3_market_usd"), 20.0) + 1e-9:
        base["reason"] = "strategy_cap"
        return base
    # No s3-specific daily-loss gate (Joel). Global bot stop still enforced in poster for non-s3.
    if not bool(cfg.get("enabled", True)):
        base["reason"] = "disabled"
        return base
    base["action"] = "buy"
    base["reason"] = "signal"
    return base


class DataApiTradeDetector:
    """Fallback adapter contract for `/v2/trades?user=...` polling."""

    source = "data_api"

    def __init__(self, data_api_url: str, wallet: str = NIULAI4):
        self.data_api_url = data_api_url.rstrip("/")
        self.wallet = wallet

    def status(self) -> dict:
        return {"detect_source": self.source, "status": "configured_stub", "url": self.data_api_url, "wallet": self.wallet}


class PolygonOrderFilledDetector:
    """Configuration seam for the fastest detector.

    The public RPC/event ABI varies by exchange deployment. The RTDS/data API
    adapters can supply the same normalized fill shape until the event topics
    are confirmed on the configured Polygon endpoint.
    """

    source = "polygon_orderfilled"

    def __init__(self, rpc_url: str, ws_url: str | None = None, exchange_addresses: list[str] | None = None):
        self.rpc_url = rpc_url
        self.ws_url = ws_url
        self.exchange_addresses = list(exchange_addresses or [])

    def status(self) -> dict:
        return {"detect_source": self.source, "status": "configured_stub", "rpc_url": self.rpc_url, "ws": bool(self.ws_url)}
