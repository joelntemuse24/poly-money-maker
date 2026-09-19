"""Shared CLOB book helpers — top-of-book price and displayed size.

Used by pathlog so REST `/book` samples parse levels the same way.
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
