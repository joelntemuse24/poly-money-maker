"""Slug, series, and Chainlink-TWAP resolution checks for lockbot.

Confirmed against Gamma on 2026-10-04 (User-Agent required):

* Event slug ``{asset}-updown-{5m|15m}-{start_ts}``.
* Series slug ``{asset}-up-or-down-{5m|15m}``.
* Current BTC/ETH/SOL/XRP 15m markets and the BTC 5m market resolve on
  ``https://data.chain.link/streams/{asset}-usd-twap-60s-streams``.
  The rules text settles Up when that Chainlink TWAP over the window
  is greater than or equal to the price at the start (``priceToBeat``,
  the 60s TWAP stamped at the open).
* ETH/SOL/XRP 15m and 5m use the same ``twap-60s`` stream. They stay
  off: after fees the wallet study has them near break-even. The
  flags can turn a book back on.
* A market whose ``resolutionSource`` does not contain ``twap-60s`` is
  not the same contract and is skipped even when its flag is on.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Optional

from buy.book import finite_float


SLUG_RE = re.compile(r"^(btc|eth|sol|xrp)-updown-(5m|15m)-(\d{10,})$")
DURATION_S = {"5m": 300.0, "15m": 900.0}
RTDS_SYMBOL = {
    "btc": "btc/usd",
    "eth": "eth/usd",
    "sol": "sol/usd",
    "xrp": "xrp/usd",
}

# Both BTC books are on. Alt books stay off until a flag is set.
SPECS: dict[str, dict[str, Any]] = {
    "btc_15m": {"asset": "btc", "duration": "15m", "lane": "btc_15m", "default": True},
    "eth_15m": {"asset": "eth", "duration": "15m", "lane": "ext", "default": False},
    "sol_15m": {"asset": "sol", "duration": "15m", "lane": "ext", "default": False},
    "xrp_15m": {"asset": "xrp", "duration": "15m", "lane": "ext", "default": False},
    "btc_5m": {"asset": "btc", "duration": "5m", "lane": "btc_5m", "default": True},
    "eth_5m": {"asset": "eth", "duration": "5m", "lane": "ext", "default": False},
    "sol_5m": {"asset": "sol", "duration": "5m", "lane": "ext", "default": False},
    "xrp_5m": {"asset": "xrp", "duration": "5m", "lane": "ext", "default": False},
}


@dataclass(frozen=True)
class LockMarket:
    asset: str
    duration: str
    lane: str
    key: str
    symbol: str
    slug: str
    series_slug: str
    condition_id: str
    question: str
    start_ts: float
    end_ts: float
    up_token: str
    dn_token: str
    resolution_source: str
    price_to_beat: Optional[float]
    resolution_ok: bool
    accepting_orders: bool
    resolved_winner: Optional[str] = None


def market_key(asset: str, duration: str) -> str:
    return f"{asset}_{duration}"


def series_slug(asset: str, duration: str) -> str:
    return f"{asset}-up-or-down-{duration}"


def event_slug(asset: str, duration: str, start_ts: float) -> str:
    return f"{asset}-updown-{duration}-{int(start_ts)}"


def parse_slug(slug: str) -> Optional[tuple[str, str, float]]:
    match = SLUG_RE.match(str(slug or ""))
    if not match:
        return None
    asset, duration, start = match.group(1), match.group(2), match.group(3)
    return asset, duration, float(start)


def rtds_symbol(asset: str) -> str:
    return RTDS_SYMBOL[asset]


def resolution_is_chainlink_twap60(url: str) -> bool:
    """True only for the Chainlink 60-second TWAP stream used as the resolver."""
    text = str(url or "").strip().lower()
    return "chain.link" in text and "twap-60s" in text


def enabled_keys(cfg: Any) -> list[str]:
    flags = (cfg or {}).get("markets") or {}
    out = []
    for key, spec in SPECS.items():
        if key in flags:
            flag = bool(flags[key])
        else:
            flag = bool(spec["default"])
        if flag:
            out.append(key)
    return out


def symbols_for(cfg: Any) -> list[str]:
    """RTDS symbols for the enabled asset/duration flags. One socket each."""
    seen: list[str] = []
    for key in enabled_keys(cfg):
        symbol = RTDS_SYMBOL[SPECS[key]["asset"]]
        if symbol not in seen:
            seen.append(symbol)
    return seen


def window_starts(now: float, duration_s: float, *, ahead: int = 1) -> list[float]:
    dur = float(duration_s)
    start = math.floor(float(now) / dur) * dur
    return [start + i * dur for i in range(0, ahead + 1)]


def market_fetch_due(
    *,
    now: float,
    fetched_at: float,
    have_market: bool,
    have_strike: bool,
    refresh_s: float,
) -> bool:
    """Whether Gamma ``/events`` should be fetched again.

    A slug we have never seen is due immediately. Once it has token ids and
    ``priceToBeat``, the next fetch waits at least 60s. Until the strike is
    published the shorter ``refresh_s`` still applies.
    """
    if not have_market or fetched_at <= 0:
        return True
    interval = float(refresh_s)
    if have_strike:
        interval = max(interval, 60.0)
    return float(now) - float(fetched_at) >= interval


def _metadata(event: dict) -> dict:
    meta = event.get("eventMetadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            return {}
    return meta if isinstance(meta, dict) else {}


def _outcomes(market: dict) -> dict[str, str]:
    raw_tokens = market.get("clobTokenIds")
    raw_names = market.get("outcomes")
    if isinstance(raw_tokens, str):
        try:
            raw_tokens = json.loads(raw_tokens)
        except json.JSONDecodeError:
            raw_tokens = []
    if isinstance(raw_names, str):
        try:
            raw_names = json.loads(raw_names)
        except json.JSONDecodeError:
            raw_names = []
    if not isinstance(raw_tokens, list) or not isinstance(raw_names, list):
        return {}
    if len(raw_tokens) != 2 or len(raw_names) != 2:
        return {}
    return {str(name).lower(): str(token) for name, token in zip(raw_names, raw_tokens)}


def gamma_winner(market: dict) -> Optional[str]:
    """Final binary payout, gated by Gamma resolution or closed status."""
    resolved = str(market.get("umaResolutionStatus") or "").lower() == "resolved"
    closed = str(market.get("closed", False)).lower() in {"true", "1", "yes"}
    if not (resolved or closed):
        return None
    names, prices = market.get("outcomes"), market.get("outcomePrices")
    try:
        names = json.loads(names) if isinstance(names, str) else names
        prices = json.loads(prices) if isinstance(prices, str) else prices
    except (ValueError, TypeError):
        return None
    if not isinstance(names, list) or not isinstance(prices, list) or len(names) != 2 or len(prices) != 2:
        return None
    payouts = {str(name).strip().lower(): finite_float(price) for name, price in zip(names, prices)}
    if set(payouts) != {"up", "down"}:
        return None
    if payouts["up"] == 1.0 and payouts["down"] == 0.0:
        return "up"
    if payouts["down"] == 1.0 and payouts["up"] == 0.0:
        return "down"
    return None


def parse_lock_event(event: Any) -> Optional[LockMarket]:
    """One Gamma ``/events?slug=`` object, or None when it is not our market."""
    if not isinstance(event, dict):
        return None
    markets = event.get("markets") or []
    market = markets[0] if isinstance(markets, list) and markets else event
    if not isinstance(market, dict):
        return None
    slug = str(market.get("slug") or event.get("slug") or "")
    parsed = parse_slug(slug)
    if parsed is None:
        return None
    asset, duration, start_ts = parsed
    mapping = _outcomes(market)
    if "up" not in mapping or "down" not in mapping:
        return None
    condition = str(market.get("conditionId") or market.get("condition_id") or "")
    if not condition:
        return None
    source = str(market.get("resolutionSource") or event.get("resolutionSource") or "")
    meta = _metadata(event)
    if not meta:
        meta = _metadata(market)
    price = finite_float(meta.get("priceToBeat")) if meta else None
    if price is None:
        price = finite_float(market.get("priceToBeat", event.get("priceToBeat")))
    if price is not None and price <= 0:
        price = None
    key = market_key(asset, duration)
    spec = SPECS.get(key)
    if spec is None:
        return None
    end_ts = start_ts + DURATION_S[duration]
    accepting = market.get("acceptingOrders", True)
    return LockMarket(
        asset=asset,
        duration=duration,
        lane=str(spec["lane"]),
        key=key,
        symbol=RTDS_SYMBOL[asset],
        slug=slug,
        series_slug=series_slug(asset, duration),
        condition_id=condition,
        question=str(market.get("question") or event.get("title") or ""),
        start_ts=start_ts,
        end_ts=end_ts,
        up_token=mapping["up"],
        dn_token=mapping["down"],
        resolution_source=source,
        price_to_beat=price,
        resolution_ok=resolution_is_chainlink_twap60(source),
        accepting_orders=bool(accepting) if isinstance(accepting, bool) else str(accepting).lower() in {"1", "true", "yes"},
        resolved_winner=gamma_winner({**event, **market}),
    )


def strike_status(
    rtds_strike: Optional[float],
    gamma_strike: Optional[float],
    *,
    match_usd: float = 0.01,
    match_rel: float = 0.0001,
) -> tuple[Optional[float], str]:
    """``(strike, reason)``.

    Gamma priceToBeat is authoritative when published. The boundary latch
    supplies the strike while the official value is unavailable.
    """
    rtds = finite_float(rtds_strike)
    gamma = finite_float(gamma_strike)
    if rtds is not None and rtds <= 0:
        rtds = None
    if gamma is not None and gamma <= 0:
        gamma = None
    if rtds is None and gamma is None:
        return None, "strike_unknown"
    if rtds is None:
        return gamma, "gamma"
    if gamma is None:
        return rtds, "rtds"
    return gamma, "gamma"


def boundary_price(
    samples: list[tuple[float, float, float]],
    start_ts: float,
    *,
    tol_s: float = 0.75,
) -> Optional[float]:
    """TWAP sample stamped on the window open. ``samples`` are ``(obs, recv, px)``."""
    best_obs = None
    best_px = None
    start = float(start_ts)
    tol = float(tol_s)
    for obs, _recv, px in samples:
        if abs(float(obs) - start) <= tol:
            if best_obs is None or float(obs) >= best_obs:
                best_obs = float(obs)
                best_px = float(px)
    return best_px
