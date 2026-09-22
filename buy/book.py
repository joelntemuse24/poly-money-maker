"""Shared CLOB book helpers — top-of-book price, displayed size, fill depth.

Used by pathlog so REST `/book` samples parse levels the same way.
Mint sell logs ``bid_fill_depth`` and uses it to skip persist when the
book covers our size, and to clip a partial loser FAK.
Do not fork a second parser in mintbot or pathlog.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Tuple


def finite_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def best_from_levels(levels: Any, side: str) -> Tuple[Optional[float], float]:
    """Best bid (max price) or ask (min price) with displayed size.

    Levels are ``{"price": ..., "size": ...}`` dicts. Requires
    ``0 < price < 1`` and ``size > 0``. Empty/unusable book → ``(None, 0.0)``.
    """
    if not levels:
        return None, 0.0
    try:
        valid = []
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = finite_float(level.get("price"))
            size = finite_float(level.get("size"))
            if (
                price is None
                or size is None
                or not 0 < price < 1
                or size <= 0
            ):
                continue
            valid.append((price, size))
        if not valid:
            return None, 0.0
        if side == "bid":
            return max(valid, key=lambda level: level[0])
        return min(valid, key=lambda level: level[0])
    except Exception:
        return None, 0.0


def best_bid_with_min_size(
    levels: Any, min_size: float = 0.0
) -> Tuple[Optional[float], float]:
    """Highest bid whose displayed size is at least ``min_size``.

    Zero-size and dust levels are skipped so a 1-lot 3¢ print cannot mask a
    real 2.9¢ book. Empty/unusable book → ``(None, 0.0)``.
    """
    need = float(min_size or 0.0)
    if not levels:
        return None, 0.0
    try:
        valid = []
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = finite_float(level.get("price"))
            size = finite_float(level.get("size"))
            if (
                price is None
                or size is None
                or not 0 < price < 1
                or size <= 0
                or size + 1e-12 < need
            ):
                continue
            valid.append((price, size))
        if not valid:
            return None, 0.0
        return max(valid, key=lambda level: level[0])
    except Exception:
        return None, 0.0


def _parsed_bid_levels(levels: Any) -> list[Tuple[float, float]]:
    """Valid bid levels merged by price, highest first."""
    merged: dict[float, float] = {}
    if not levels:
        return []
    try:
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = finite_float(level.get("price"))
            size = finite_float(level.get("size"))
            if (
                price is None
                or size is None
                or not 0 < price < 1
                or size <= 0
            ):
                continue
            key = round(price, 4)
            merged[key] = merged.get(key, 0.0) + size
    except Exception:
        return []
    return sorted(merged.items(), key=lambda item: item[0], reverse=True)


def bid_fill_depth(
    levels: Any,
    limit: Optional[float],
    *,
    tick: float = 0.01,
    extra_ticks: int = 2,
) -> dict:
    """Cumulative bid size a sell FAK at ``limit`` can take.

    A CLOB SELL FAK at ``limit`` matches resting bids with ``price >= limit``.
    ``ladder`` is compact cumulative depth at limit, limit-1¢, limit-2¢
    (skipping non-positive prices). Log-only — does not change sell policy.
    """
    parsed = _parsed_bid_levels(levels)
    best_bid: Optional[float] = parsed[0][0] if parsed else None
    best_bid_size = float(parsed[0][1]) if parsed else 0.0

    def depth_at(px: float) -> float:
        return round(
            float(sum(size for price, size in parsed if price + 1e-12 >= px)),
            4,
        )

    lim = finite_float(limit)
    depth_at_limit = depth_at(lim) if lim is not None and lim > 0 else 0.0
    ladder: list[dict] = []
    if lim is not None and lim > 0:
        step = float(tick or 0.01)
        for i in range(int(extra_ticks) + 1):
            px = round(lim - step * i, 4)
            if px <= 0:
                break
            ladder.append({"price": px, "depth": depth_at(px)})
    elif parsed:
        cum = 0.0
        for price, size in parsed[:3]:
            cum = round(cum + size, 4)
            ladder.append({"price": price, "depth": cum})
    return {
        "best_bid": best_bid,
        "best_bid_size": best_bid_size,
        "depth_at_limit": depth_at_limit,
        "ladder": ladder,
    }
