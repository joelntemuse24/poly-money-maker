"""Hedge persist gate (no I/O). Toxic dumps stay instant."""

from __future__ import annotations

from typing import Optional, Tuple


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
