"""Classify why an in-window 5m market did not arm a BUY (no I/O)."""

from __future__ import annotations

from typing import NamedTuple, Optional


class EntryBand(NamedTuple):
    """Price band that applies at this time-to-market."""

    min_price: float
    max_price: float
    min_exclusive: bool
    name: str

    @property
    def retry_min_price(self) -> float:
        """FAK retries abort when ``ask < min_price``; bump exclusive floors."""
        if self.min_exclusive:
            return float(self.min_price) + 1e-12
        return float(self.min_price)


def entry_band_for_seconds(
    seconds_left: float,
    *,
    late_start_s: float,
    late_min: float,
    late_max: float,
    early_start_s: float,
    early_min: float,
    early_max: float,
) -> Optional[EntryBand]:
    """Return the 5m entry band for this TTM, or None outside every buy window.

    Late window (``seconds_left <= late_start_s``): inclusive ``late_min``–
    ``late_max`` (live 75–90¢ in the last 120s).

    Early window (``late_start_s < seconds_left <= early_start_s``): ask must
    be *above* ``early_min`` (live: above 90¢ in the first 3 minutes of a 5m
    market) and at most ``early_max``. Empty when ``early_start_s`` is not
    greater than ``late_start_s``.
    """
    try:
        ttm = float(seconds_left)
        late_s = float(late_start_s)
        early_s = float(early_start_s)
    except (TypeError, ValueError):
        return None
    if ttm <= 0:
        return None
    if ttm <= late_s + 1e-12:
        return EntryBand(
            float(late_min), float(late_max), False, "late",
        )
    if early_s > late_s + 1e-12 and ttm <= early_s + 1e-12:
        return EntryBand(
            float(early_min), float(early_max), True, "early",
        )
    return None


def ask_in_entry_band(
    ask: Optional[float],
    min_price: float,
    max_price: float,
    *,
    min_exclusive: bool = False,
) -> bool:
    """True when ``ask`` is inside the active entry band."""
    if ask is None:
        return False
    try:
        ask_f = float(ask)
    except (TypeError, ValueError):
        return False
    lo = float(min_price)
    hi = float(max_price)
    if min_exclusive:
        if ask_f <= lo + 1e-12:
            return False
    elif ask_f < lo - 1e-12:
        return False
    if ask_f > hi + 1e-12:
        return False
    return True


def ask_band_reason(
    ask: Optional[float],
    threshold: float,
    max_price: float,
    min_exclusive: bool = False,
) -> str:
    """Why a winning-leg ask failed the active entry band."""
    if ask is None:
        return "no_ask"
    try:
        ask_f = float(ask)
    except (TypeError, ValueError):
        return "no_ask"
    if min_exclusive:
        if ask_f <= float(threshold) + 1e-12:
            return "ask_below_band"
    elif ask_f < float(threshold) - 1e-12:
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
    min_exclusive: bool = False,
) -> Optional[str]:
    """Return a skip reason, or None when a buy is armed.

    Used both for live throttled logs and for tests. Does not cover
    pre-book gates (outside the buy windows, already held, stale discovery).
    """
    if up_buy or dn_buy:
        return None
    if up_winning:
        if not up_ask_ok:
            return ask_band_reason(
                up_ask, threshold, max_price, min_exclusive=min_exclusive,
            )
        if not up_consensus:
            return "no_consensus"
        return "blocked_other"
    if dn_winning:
        if not dn_ask_ok:
            return ask_band_reason(
                dn_ask, threshold, max_price, min_exclusive=min_exclusive,
            )
        if not dn_consensus:
            return "no_consensus"
        return "blocked_other"
    return "ambiguous_or_no_winner"
