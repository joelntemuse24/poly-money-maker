"""Which BTC 15m windows the recorder should subscribe to.

The current window, the next one, and a window that ended inside
``grace_s`` (so the last prints are not cut off when Gamma drops it).
"""

from __future__ import annotations

from typing import Iterable, Sequence


SERIES_15M = "btc-up-or-down-15m"
SLUG_PREFIX = "btc-updown-15m-"


def _is_15m(market: object) -> bool:
    series = str(getattr(market, "series_slug", "") or "")
    slug = str(getattr(market, "slug", "") or "")
    return series == SERIES_15M or slug.startswith(SLUG_PREFIX)


def select_bags(markets: Iterable[object], now: float, *, grace_s: float = 30.0) -> list:
    """Current window, the next window, and a just-closed window inside grace."""
    try:
        moment = float(now)
        grace = float(grace_s)
    except (TypeError, ValueError):
        return []
    if grace < 0:
        grace = 0.0
    rows = [market for market in markets if _is_15m(market)]
    rows.sort(key=lambda market: (float(getattr(market, "start_ts", 0.0) or 0.0), str(getattr(market, "slug", ""))))
    chosen: list = []
    for market in rows:
        start = float(getattr(market, "start_ts", 0.0) or 0.0)
        end = float(getattr(market, "end_ts", 0.0) or 0.0)
        if end + grace <= moment:
            continue
        if start > moment:
            chosen.append(market)
            break
        chosen.append(market)
    out = []
    seen = set()
    for market in chosen:
        slug = str(getattr(market, "slug", ""))
        if slug in seen:
            continue
        seen.add(slug)
        out.append(market)
    return out


def bags_to_record(
    discovered: Sequence[object],
    previous: Sequence[object],
    now: float,
    *,
    grace_s: float = 30.0,
) -> list:
    """Keep a window Gamma just dropped until ``end + grace``."""
    merged = list(discovered)
    have = {str(getattr(market, "slug", "")) for market in discovered}
    try:
        moment = float(now)
        grace = float(grace_s)
    except (TypeError, ValueError):
        return select_bags(merged, now, grace_s=grace_s)
    for market in previous:
        slug = str(getattr(market, "slug", ""))
        if slug in have:
            continue
        end = float(getattr(market, "end_ts", 0.0) or 0.0)
        if moment < end + grace:
            merged.append(market)
    return select_bags(merged, moment, grace_s=grace)


def market_key(markets: Sequence[object]) -> tuple:
    """Stable identity of the subscribed set. Unchanged books do not reconnect."""
    rows = []
    for market in markets:
        rows.append(
            (
                str(getattr(market, "slug", "")),
                str(getattr(market, "up_token", "")),
                str(getattr(market, "dn_token", "")),
                float(getattr(market, "start_ts", 0.0) or 0.0),
            )
        )
    return tuple(sorted(rows))
