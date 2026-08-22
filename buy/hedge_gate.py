"""Hedge persist gate (no I/O). Toxic dumps stay instant."""

from __future__ import annotations

from typing import Optional, Tuple


def hedge_winner_rest_rememberable(
    bid: Optional[float],
    *,
    threshold: float,
    cushion: float = 0.10,
) -> bool:
    """True when *bid* is comfortably above the hedge line.

    Near-threshold books (≤ threshold + cushion) must keep RESTing so a
    71–80¢ fade is not ignored after WS drops. Toxic dumps must not cache.
    """
    if bid is None:
        return False
    try:
        price = float(bid)
        line = float(threshold)
        pad = float(cushion)
    except (TypeError, ValueError):
        return False
    if pad < 0:
        pad = 0.0
    return price > line + pad + 1e-12


def hedge_winner_rest_fresh(
    last_bid: Optional[float],
    last_ts: Optional[float],
    *,
    now_s: float,
    threshold: float,
    ttl_s: float,
    cushion: float = 0.10,
) -> bool:
    """True when a recent winner bid is still young enough to skip REST.

    Fail-closed: missing/stale/near-threshold/disabled TTL → REST again.
    Never use this for ``toxic_fill`` (those still need a live bid).
    """
    try:
        wait = float(ttl_s or 0)
    except (TypeError, ValueError):
        wait = 0.0
    if wait <= 1e-12 or last_ts is None:
        return False
    if not hedge_winner_rest_rememberable(
        last_bid, threshold=threshold, cushion=cushion,
    ):
        return False
    try:
        age = float(now_s) - float(last_ts)
    except (TypeError, ValueError):
        return False
    return 0.0 <= age < wait - 1e-12


def hedge_persist_ready(
    qualifies: bool,
    *,
    now_s: float,
    armed_ts: Optional[float],
    persist_s: float,
    toxic: bool = False,
) -> Tuple[bool, Optional[float], str]:
    """Whether a qualifying hedge book may sell yet.

    Returns ``(fire, new_armed_ts, why)``.

    ``persist_s`` ≤ 0 or ``toxic`` sells on the first qualifying tick.
    A failed book clears the arm so a one-tick dip does not count.
    """
    if not qualifies:
        return False, None, "reset"
    try:
        wait = float(persist_s or 0)
    except (TypeError, ValueError):
        wait = 0.0
    if toxic or wait <= 1e-12:
        return True, armed_ts, "immediate"
    if armed_ts is None:
        return False, float(now_s), "armed"
    if float(now_s) - float(armed_ts) < wait - 1e-12:
        return False, float(armed_ts), "waiting"
    return True, float(armed_ts), "ready"
