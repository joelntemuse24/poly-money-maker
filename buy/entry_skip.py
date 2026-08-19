"""Classify why an in-window 5m market did not arm a BUY (no I/O)."""

from __future__ import annotations

from typing import Optional


def ask_band_reason(
    ask: Optional[float],
    threshold: float,
    max_price: float,
) -> str:
    """Why a winning-leg ask failed the 75–90¢ band."""
    if ask is None:
        return "no_ask"
    try:
        ask_f = float(ask)
    except (TypeError, ValueError):
        return "no_ask"
    if ask_f < float(threshold) - 1e-12:
        return "ask_below_band"
    if ask_f > float(max_price) + 1e-12:
        return "ask_above_band"
    return "ask_out_of_band"


def window_no_buy_reason(
    *,
    up_ask: Optional[float],
    dn_ask: Optional[float],
    up_winning: bool,
    dn_winning: bool,
    up_ask_ok: bool,
    dn_ask_ok: bool,
    up_consensus: bool,
    dn_consensus: bool,
    up_buy: bool,
    dn_buy: bool,
    threshold: float,
    max_price: float,
) -> Optional[str]:
    """Return a skip reason, or None when a buy is armed.

    Used both for live throttled logs and for tests. Does not cover
    pre-book gates (not in last 120s, already held, stale discovery).
    """
    if up_buy or dn_buy:
        return None
    if up_winning:
        if not up_ask_ok:
            return ask_band_reason(up_ask, threshold, max_price)
        if not up_consensus:
            return "no_consensus"
        return "blocked_other"
    if dn_winning:
        if not dn_ask_ok:
            return ask_band_reason(dn_ask, threshold, max_price)
        if not dn_consensus:
            return "no_consensus"
        return "blocked_other"
    return "ambiguous_or_no_winner"
