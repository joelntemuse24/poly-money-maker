"""Book parsing, expired exposure exclusion, and legacy settlement arithmetic."""
from __future__ import annotations

import math
import time
from typing import Any, Optional, Sequence
from buy.book import finite_float


def parse_levels(levels: Any, side: str) -> list[tuple[float, float]]:
    """Valid book levels. Asks are cheapest first, bids richest first."""
    merged: dict[float, float] = {}
    if not levels:
        return []
    for level in levels:
        if isinstance(level, dict):
            price = finite_float(level.get("price"))
            size = finite_float(level.get("size"))
        elif isinstance(level, (tuple, list)) and len(level) >= 2:
            price = finite_float(level[0])
            size = finite_float(level[1])
        else:
            continue
        if price is None or size is None or not 0 < price < 1 or size <= 0:
            continue
        key = round(price, 4)
        merged[key] = merged.get(key, 0.0) + size
    rows = list(merged.items())
    rows.sort(key=lambda item: item[0], reverse=(side == "bid"))
    return rows


def open_exposure_usd(positions: Sequence[dict], now: Optional[float] = None) -> float:
    """Cost of unsettled positions whose windows are still open."""
    now = time.time() if now is None else float(now)
    total = 0.0
    for pos in positions or []:
        if not isinstance(pos, dict) or pos.get("settled_ts"):
            continue
        end = finite_float(pos.get("end_ts"))
        if end is not None and end <= now:
            continue
        total += max(0.0, _num(pos.get("cost"), 0.0))
    return total


def _num(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def settle_pnl(
    *,
    side: str,
    shares: float,
    cost: float,
    fee: float,
    final_twap: float,
    strike: float,
) -> dict:
    """Hold-to-settlement P&L. Up wins ties (final TWAP >= strike)."""
    up_wins = float(final_twap) + 1e-12 >= float(strike)
    won = up_wins if str(side) == "up" else (not up_wins)
    payout = float(shares) * (1.0 if won else 0.0)
    pnl = payout - float(cost) - float(fee)
    return {"won": won, "up_wins": up_wins, "payout": payout, "pnl": pnl}
