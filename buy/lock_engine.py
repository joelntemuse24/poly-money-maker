"""In-memory view for one lockbot tick. No network."""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence

from buy.lock_fair import fair_up, resample_1s, sigma_from_prices
from buy.lock_markets import LockMarket, boundary_price, strike_status


def path_covered(
    samples: Sequence[tuple[float, float, float]],
    start: float,
    end: float,
    max_gap: float,
) -> bool:
    """Every moment from ``start`` to ``end`` is within ``max_gap`` of a sample."""
    if end < start:
        return False
    obs = sorted(
        float(row[0])
        for row in samples
        if start - max_gap - 1e-9 <= float(row[0]) <= end + 1e-6
    )
    if not obs:
        return False
    cursor = float(start)
    gap = float(max_gap)
    for stamp in obs:
        if stamp > cursor + gap:
            return False
        if stamp > cursor:
            cursor = stamp
    return float(end) - cursor <= gap


def elapsed_prices(
    samples: Sequence[tuple[float, float, float]],
    settle_start: float,
    now: float,
) -> list[float]:
    """One Chainlink print per whole second inside the settlement window."""
    rows = [
        (float(obs), float(px))
        for obs, _recv, px in samples
        if settle_start - 1.0 <= float(obs) <= float(now) + 1e-6
    ]
    if not rows:
        return []
    rows.sort()
    start = math.floor(float(settle_start))
    end = math.floor(float(now))
    out: list[float] = []
    idx = 0
    carry: Optional[float] = None
    # A print just before the window can seed the first second.
    for obs, px in rows:
        if obs < settle_start:
            carry = px
        else:
            break
    n = len(rows)
    for sec in range(int(start), int(end) + 1):
        limit = min(float(now), sec + 1.0 - 1e-9)
        while idx < n and rows[idx][0] <= limit:
            if rows[idx][0] >= settle_start - 1e-9:
                carry = rows[idx][1]
            idx += 1
        if carry is not None and limit >= settle_start:
            out.append(carry)
    return out


def _num(cfg: dict, key: str, default: float) -> float:
    try:
        value = float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def build_view(
    market: LockMarket,
    *,
    now: float,
    live_hist: Sequence[tuple[float, float, float]],
    twap_hist: Sequence[tuple[float, float, float]],
    up_book: Optional[dict] = None,
    dn_book: Optional[dict] = None,
    gamma_strike: Optional[float] = None,
    cfg: Optional[dict] = None,
    market_enabled: bool = True,
) -> dict:
    """Oracle + book snapshot the entry gate can score without I/O."""
    cfg = cfg or {}
    settle_s = _num(cfg, "settle_window_s", 60.0)
    gap = _num(cfg, "elapsed_max_gap_s", 3.0)
    gamma = gamma_strike if gamma_strike is not None else market.price_to_beat
    rtds_strike = boundary_price(
        list(twap_hist),
        market.start_ts,
        tol_s=_num(cfg, "strike_tol_s", 0.75),
    )
    strike, strike_reason = strike_status(
        rtds_strike,
        gamma,
        match_usd=_num(cfg, "strike_match_usd", 0.01),
        match_rel=_num(cfg, "strike_match_rel", 0.0001),
    )
    live_rows = sorted(live_hist, key=lambda row: float(row[0]))
    twap_rows = sorted(twap_hist, key=lambda row: float(row[0]))
    live = live_rows[-1] if live_rows else None
    twap = twap_rows[-1] if twap_rows else None
    # Histories are kept in obs order. The fresh sample is the newest obs,
    # which is the last row after a merge. Receive time is that row's.
    settle_start = market.end_ts - settle_s
    covered = path_covered(live_rows, settle_start, now, gap) if now > settle_start else True
    grid = elapsed_prices(live_rows, settle_start, now) if now > settle_start else []
    levels = resample_1s([(obs, px) for obs, _recv, px in live_rows])
    sigma, nrets = sigma_from_prices(
        levels,
        short_n=int(_num(cfg, "vol_short_s", 300)),
        long_n=int(_num(cfg, "vol_long_s", 900)),
    )
    tau = market.end_ts - float(now)
    expected = None
    p_up = None
    if (
        strike is not None
        and grid
        and covered
        and nrets >= int(_num(cfg, "vol_min_samples", 60))
        and tau < settle_s
    ):
        fair = fair_up(
            strike=strike,
            elapsed_prices=grid,
            tau_s=max(tau, 0.0),
            sigma=sigma,
            noise_frac=_num(cfg, "noise_frac", 0.00002),
            settle_s=settle_s,
        )
        p_up = fair["p_up"]
        expected = fair["expected"]
        sigma = fair["sigma"]
    elif strike is not None and live is not None and tau >= settle_s and nrets >= 2:
        fair = fair_up(
            strike=strike,
            elapsed_prices=[live[2]],
            tau_s=tau,
            sigma=sigma,
            noise_frac=_num(cfg, "noise_frac", 0.00002),
            settle_s=settle_s,
        )
        p_up = fair["p_up"]
        expected = fair["expected"]

    def book(raw: Optional[dict]) -> dict:
        raw = raw or {}
        return {
            "asks": raw.get("asks") or [],
            "bids": raw.get("bids") or [],
            "recv_ts": raw.get("recv_ts"),
        }

    return {
        "now": float(now),
        "slug": market.slug,
        "asset": market.asset,
        "duration": market.duration,
        "lane": market.lane,
        "key": market.key,
        "condition_id": market.condition_id,
        "start_ts": market.start_ts,
        "end_ts": market.end_ts,
        "up_token": market.up_token,
        "dn_token": market.dn_token,
        "symbol": market.symbol,
        "market_enabled": bool(market_enabled),
        "resolution_ok": market.resolution_ok,
        "resolution_source": market.resolution_source,
        "strike": strike,
        "strike_reason": strike_reason,
        "gamma_strike": gamma,
        "rtds_strike": rtds_strike,
        "live": None if live is None else live[2],
        "live_recv_ts": None if live is None else live[1],
        "live_obs_ts": None if live is None else live[0],
        "twap": None if twap is None else twap[2],
        "twap_recv_ts": None if twap is None else twap[1],
        "twap_obs_ts": None if twap is None else twap[0],
        "coverage_ok": covered,
        "vol_samples": nrets,
        "sigma": sigma,
        "p_up": p_up,
        "expected": expected,
        "up": book(up_book),
        "down": book(dn_book),
    }


def _book_snapshot(raw: Optional[dict]) -> dict:
    raw = raw or {}
    return {
        "asks": raw.get("asks") or [],
        "bids": raw.get("bids") or [],
        "recv_ts": raw.get("recv_ts"),
    }


def s2_quote_view(
    market: LockMarket,
    *,
    now: float,
    up_book: Optional[dict] = None,
    dn_book: Optional[dict] = None,
    strike: Optional[float] = None,
) -> dict:
    """Books and identity for a strategy-2 tick. No Chainlink resample.

    Strategy 2's default rule is the Binance move and the ask. The full
    ``build_view`` path (1-second resample and sigma) stays on the
    once-a-second strategy-1 tick, and on strategy 2 only when the
    optional q filter is on.
    """
    return {
        "now": float(now),
        "slug": market.slug,
        "asset": market.asset,
        "duration": market.duration,
        "lane": market.lane,
        "key": market.key,
        "condition_id": market.condition_id,
        "start_ts": market.start_ts,
        "end_ts": market.end_ts,
        "up_token": market.up_token,
        "dn_token": market.dn_token,
        "symbol": market.symbol,
        "market_enabled": True,
        "resolution_ok": market.resolution_ok,
        "resolution_source": market.resolution_source,
        "strike": strike,
        "up": _book_snapshot(up_book),
        "down": _book_snapshot(dn_book),
    }
