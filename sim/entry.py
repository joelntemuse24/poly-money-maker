from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .fills import FillResult


@dataclass
class SetEntryEstimate:
    requested: float
    up: FillResult
    dn: FillResult
    paired_shares: float
    imbalance: float
    total_notional: float
    set_cost: Optional[float]
    complete: bool
    within_cost: bool
    admissible: bool
    reason: str


def _parse_asks(asks: Sequence[dict]) -> List[Tuple[float, float]]:
    out = []
    for ask in asks or []:
        try:
            price = float(ask.get("price", 0))
            size = float(ask.get("size", 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 < price <= 1 and size > 0:
            out.append((price, size))
    out.sort(key=lambda level: level[0])
    return out


def simulate_fak_buy(
    *,
    size: float,
    asks: Sequence[dict],
    limit_price: Optional[float],
    model: str = "depth",
    slippage: float = 0.0,
) -> FillResult:
    if size <= 0:
        return FillResult(0.0, 0.0, 0.0, 0, "zero_size")

    levels = _parse_asks(asks)
    if not levels:
        return FillResult(0.0, 0.0, 0.0, 0, "empty_book")

    adverse = max(0.0, float(slippage or 0.0))
    ceiling = None if limit_price is None else min(1.0, float(limit_price))
    match_ceiling = None if ceiling is None else ceiling - adverse

    if model == "best_ask":
        best_price, best_size = levels[0]
        if match_ceiling is not None and best_price > match_ceiling + 1e-12:
            return FillResult(0.0, 0.0, 0.0, 0, "best_above_limit")
        if best_size + 1e-12 < size:
            return FillResult(0.0, 0.0, 0.0, 0, "insufficient_top_size")
        price = min(1.0, best_price + adverse)
        return FillResult(size, price, size * price, 1, "filled_best_ask")

    if model == "best_ask_partial":
        best_price, best_size = levels[0]
        if match_ceiling is not None and best_price > match_ceiling + 1e-12:
            return FillResult(0.0, 0.0, 0.0, 0, "best_above_limit")
        filled = min(size, best_size)
        price = min(1.0, best_price + adverse)
        return FillResult(filled, price, filled * price, 1, "filled" if filled + 1e-12 >= size else "partial_top")

    remaining = size
    notional = 0.0
    levels_used = 0
    for price, available in levels:
        if match_ceiling is not None and price > match_ceiling + 1e-12:
            break
        take = min(remaining, available)
        if take <= 0:
            continue
        execution_price = min(1.0, price + adverse)
        notional += take * execution_price
        remaining -= take
        levels_used += 1
        if remaining <= 1e-12:
            break

    filled = size - remaining
    if filled <= 1e-12:
        return FillResult(0.0, 0.0, 0.0, 0, "no_match")
    average = notional / filled
    reason = "filled" if remaining <= 1e-12 else "partial"
    return FillResult(filled, average, notional, levels_used, reason)


def estimate_set_cost_from_books(
    *,
    shares: float,
    up_asks: Sequence[dict],
    dn_asks: Sequence[dict],
    max_set_cost: float,
    limit_price: Optional[float] = None,
    model: str = "depth",
    slippage: float = 0.0,
) -> SetEntryEstimate:
    up = simulate_fak_buy(
        size=shares,
        asks=up_asks,
        limit_price=limit_price,
        model=model,
        slippage=slippage,
    )
    dn = simulate_fak_buy(
        size=shares,
        asks=dn_asks,
        limit_price=limit_price,
        model=model,
        slippage=slippage,
    )
    paired = min(up.filled, dn.filled)
    imbalance = abs(up.filled - dn.filled)
    total_notional = up.notional + dn.notional
    complete = up.filled + 1e-12 >= shares and dn.filled + 1e-12 >= shares
    set_cost = total_notional / shares if complete and shares > 0 else None
    within_cost = set_cost is not None and set_cost <= float(max_set_cost) + 1e-12
    admissible = complete and within_cost
    if not complete:
        reason = "incomplete_up" if up.filled + 1e-12 < shares else "incomplete_dn"
        if up.filled + 1e-12 < shares and dn.filled + 1e-12 < shares:
            reason = "incomplete_both"
    elif not within_cost:
        reason = "set_cost"
    else:
        reason = "admissible"
    return SetEntryEstimate(
        requested=shares,
        up=up,
        dn=dn,
        paired_shares=paired,
        imbalance=imbalance,
        total_notional=total_notional,
        set_cost=set_cost,
        complete=complete,
        within_cost=within_cost,
        admissible=admissible,
        reason=reason,
    )
