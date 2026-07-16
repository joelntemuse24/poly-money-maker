"""Realistic FAK fill simulation against a live order book snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


@dataclass
class FillResult:
    filled: float
    avg_price: float
    notional: float
    levels_used: int
    reason: str


def _parse_bids(bids: Sequence[dict]) -> List[Tuple[float, float]]:
    """Return bids sorted best-first (highest price first)."""
    out = []
    for b in bids or []:
        try:
            px = float(b.get("price", 0))
            sz = float(b.get("size", 0))
        except (TypeError, ValueError):
            continue
        if px > 0 and sz > 0:
            out.append((px, sz))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def simulate_fak_sell(
    *,
    size: float,
    bids: Sequence[dict],
    limit_price: Optional[float],
    model: str = "depth",
    slippage: float = 0.0,
    seconds_left: float = 999.0,
    no_fill_after_s: float = 0.0,
) -> FillResult:
    """Simulate selling `size` shares into the bid book (FAK semantics).

    - depth: walk all bids at/above limit until size filled or book exhausted
    - best_bid: all-or-nothing at best bid if best >= limit and size available
    - best_bid_partial: fill only top-of-book size at best bid
    """
    if size <= 0:
        return FillResult(0.0, 0.0, 0.0, 0, "zero_size")
    if no_fill_after_s > 0 and seconds_left <= no_fill_after_s:
        return FillResult(0.0, 0.0, 0.0, 0, "too_late")

    levels = _parse_bids(bids)
    if not levels:
        return FillResult(0.0, 0.0, 0.0, 0, "empty_book")

    # Effective minimum price we accept (FAK limit). None = take any bid.
    floor = None if limit_price is None else max(0.0, float(limit_price) - float(slippage or 0.0))

    if model == "best_bid":
        best_px, best_sz = levels[0]
        if floor is not None and best_px < floor:
            return FillResult(0.0, 0.0, 0.0, 0, "best_below_limit")
        if best_sz + 1e-12 < size:
            return FillResult(0.0, 0.0, 0.0, 0, "insufficient_top_size")
        px = max(0.0, best_px - float(slippage or 0.0))
        return FillResult(size, px, size * px, 1, "filled_best_bid")

    if model == "best_bid_partial":
        best_px, best_sz = levels[0]
        if floor is not None and best_px < floor:
            return FillResult(0.0, 0.0, 0.0, 0, "best_below_limit")
        filled = min(size, best_sz)
        px = max(0.0, best_px - float(slippage or 0.0))
        if filled <= 0:
            return FillResult(0.0, 0.0, 0.0, 0, "no_size")
        return FillResult(filled, px, filled * px, 1, "partial_top")

    # Default: depth walk (most realistic FAK)
    remaining = size
    notional = 0.0
    used = 0
    for px, sz in levels:
        if floor is not None and px < floor:
            break
        take = min(remaining, sz)
        if take <= 0:
            continue
        exec_px = max(0.0, px - float(slippage or 0.0))
        notional += take * exec_px
        remaining -= take
        used += 1
        if remaining <= 1e-12:
            break

    filled = size - remaining
    if filled <= 1e-12:
        return FillResult(0.0, 0.0, 0.0, 0, "no_match")
    avg = notional / filled
    reason = "filled" if remaining <= 1e-12 else "partial"
    return FillResult(filled, avg, notional, used, reason)
