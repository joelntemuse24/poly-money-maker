"""Classify why an in-window 5m market did not arm a BUY (no I/O)."""

from __future__ import annotations

from typing import Iterable, List, NamedTuple, Optional, Sequence


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
    early_95_start_s: float = 0.0,
    early_95_min_s: float = 0.0,
    early_95_min: float = 0.95,
) -> Optional[EntryBand]:
    """Primary band for this TTM (late, then >90 early, then ≥95).

    Prefer ``applicable_entry_bands`` when more than one window can fire
    (last 120s 75–90¢ and first-4-min ≥95¢ overlap).
    """
    bands = applicable_entry_bands(
        seconds_left,
        late_start_s=late_start_s,
        late_min=late_min,
        late_max=late_max,
        early_start_s=early_start_s,
        early_min=early_min,
        early_max=early_max,
        early_95_start_s=early_95_start_s,
        early_95_min_s=early_95_min_s,
        early_95_min=early_95_min,
    )
    return bands[0] if bands else None


def applicable_entry_bands(
    seconds_left: float,
    *,
    late_start_s: float,
    late_min: float,
    late_max: float,
    early_start_s: float,
    early_min: float,
    early_max: float,
    early_95_start_s: float,
    early_95_min_s: float,
    early_95_min: float,
) -> List[EntryBand]:
    """Every 5m entry band that is open at this TTM (may be more than one).

    Late: ``seconds_left <= late_start_s`` → inclusive 75–90¢.

    Early >90: ``late_start_s < seconds_left <= early_start_s`` → ask *above*
    90¢ (first 3 minutes of a 5m market).

    Early ≥95: ``early_95_min_s <= seconds_left <= early_95_start_s`` → ask
    at least 95¢ (first 4 minutes of a 5m market). Overlaps the late window
    for TTM 60–120s so 95¢+ can still buy there.
    """
    try:
        ttm = float(seconds_left)
        late_s = float(late_start_s)
        early_s = float(early_start_s)
        early_95_s = float(early_95_start_s)
        early_95_floor = float(early_95_min_s)
    except (TypeError, ValueError):
        return []
    if ttm <= 0:
        return []
    bands: List[EntryBand] = []
    if ttm <= late_s + 1e-12:
        bands.append(EntryBand(float(late_min), float(late_max), False, "late"))
    if early_s > late_s + 1e-12 and late_s + 1e-12 < ttm <= early_s + 1e-12:
        bands.append(EntryBand(float(early_min), float(early_max), True, "early"))
    if (
        early_95_s > early_95_floor + 1e-12
        and early_95_floor - 1e-12 <= ttm <= early_95_s + 1e-12
    ):
        bands.append(EntryBand(
            float(early_95_min), float(early_max), False, "early_95",
        ))
    return bands


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


def ask_in_any_band(ask: Optional[float], bands: Sequence[EntryBand]) -> bool:
    return any(
        ask_in_entry_band(
            ask, band.min_price, band.max_price,
            min_exclusive=band.min_exclusive,
        )
        for band in bands
    )


def select_entry_band(
    ask: Optional[float], bands: Sequence[EntryBand],
) -> Optional[EntryBand]:
    """Widest matching band (lowest retry floor) for FAK min/max pins."""
    matching = [
        band for band in bands
        if ask_in_entry_band(
            ask, band.min_price, band.max_price,
            min_exclusive=band.min_exclusive,
        )
    ]
    if not matching:
        return None
    return min(matching, key=lambda band: (band.retry_min_price, -band.max_price))


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


def union_ask_band_reason(
    ask: Optional[float], bands: Iterable[EntryBand],
) -> str:
    """Skip reason against the union of open bands (holes are out-of-band)."""
    band_list = list(bands)
    if ask is None:
        return "no_ask"
    try:
        ask_f = float(ask)
    except (TypeError, ValueError):
        return "no_ask"
    if not band_list:
        return "ask_out_of_band"
    if ask_in_any_band(ask_f, band_list):
        return "ask_out_of_band"
    floors = [band.retry_min_price for band in band_list]
    caps = [band.max_price for band in band_list]
    if ask_f < min(floors) - 1e-12:
        return "ask_below_band"
    if ask_f > max(caps) + 1e-12:
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
    bands: Optional[Sequence[EntryBand]] = None,
) -> Optional[str]:
    """Return a skip reason, or None when a buy is armed.

    Used both for live throttled logs and for tests. Does not cover
    pre-book gates (outside the buy windows, already held, stale discovery).
    """
    if up_buy or dn_buy:
        return None

    def _band_reason(ask: Optional[float]) -> str:
        if bands:
            return union_ask_band_reason(ask, bands)
        return ask_band_reason(
            ask, threshold, max_price, min_exclusive=min_exclusive,
        )

    if up_winning:
        if not up_ask_ok:
            return _band_reason(up_ask)
        if not up_consensus:
            return "no_consensus"
        return "blocked_other"
    if dn_winning:
        if not dn_ask_ok:
            return _band_reason(dn_ask)
        if not dn_consensus:
            return "no_consensus"
        return "blocked_other"
    return "ambiguous_or_no_winner"
