"""Hedge persist gate and CLOB tick helpers (no I/O). Toxic dumps stay instant."""

from __future__ import annotations

import re
from typing import Optional, Tuple

_CLOB_TICKS = (0.1, 0.01, 0.005, 0.0025, 0.001, 0.0001)
_MIN_TICK_RE = re.compile(r"minimum is\s+(0\.\d+)", re.IGNORECASE)


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


def _known_clob_tick(raw: float) -> Optional[float]:
    for known in _CLOB_TICKS:
        if abs(float(raw) - known) < 1e-12:
            return known
    return None


def hedge_market_tick(reported, expected=0.001) -> float:
    """Tick the CLOB will accept for a 5m hedge FAK.

    Honor a *coarser* market tick. Forcing 0.001 on a 0.01 book rejects the
    signed order (``invalid tick size (0.001), minimum is 0.01``) and the
    dump never sells (live 22 Aug 11:40: persist fired at 61/62, then
    ``[EXIT FAIL]``).

    The 21 Aug unmatched 0.51-into-0.53 hole was ``hedge_undercut_ticks=2``
    on a 0.01 tick, not "must post 0.001". Live undercut stays 0: sell at
    the live bid aligned to this tick.
    """
    try:
        exp = float(expected) if expected not in (None, "") else 0.001
    except (TypeError, ValueError):
        exp = 0.001
    if exp <= 0:
        exp = 0.001
    try:
        raw = float(reported) if reported not in (None, "") else exp
    except (TypeError, ValueError):
        raw = exp
    if raw <= 0:
        raw = exp
    known = _known_clob_tick(raw)
    if known is not None:
        raw = known
    known_exp = _known_clob_tick(exp)
    if known_exp is not None:
        exp = known_exp
    return max(raw, exp)


def clob_min_tick_from_error(error) -> Optional[float]:
    """Parse ``invalid tick size (0.001), minimum is 0.01`` from a CLOB error."""
    match = _MIN_TICK_RE.search(str(error or ""))
    if not match:
        return None
    try:
        tick = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if tick <= 0:
        return None
    return _known_clob_tick(tick) or tick


def hedge_tick_after_build_error(current_tick, error) -> Optional[float]:
    """If CLOB rejected a too-fine tick, return the minimum we must retry with."""
    minimum = clob_min_tick_from_error(error)
    if minimum is None:
        return None
    try:
        current = float(current_tick)
    except (TypeError, ValueError):
        current = 0.0
    if minimum > current + 1e-12:
        return minimum
    return None
