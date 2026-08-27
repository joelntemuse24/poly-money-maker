"""Classify why an in-window 5m or hourly market did not arm a BUY (no I/O)."""

from __future__ import annotations

from typing import Iterable, List, NamedTuple, Optional, Sequence


class EntryBand(NamedTuple):
    """Price band that applies at this time-to-market."""

    min_price: float
    max_price: float
    min_exclusive: bool
    name: str
    # 0 → FAK limit is ``max_price`` (5m late 90¢ / early 99¢). Hourly slice A
    # matches (0.93, 0.95] when C is also open but still limits the FAK at 99¢.
    limit_price: float = 0.0

    @property
    def retry_min_price(self) -> float:
        """FAK retries abort when ``ask < min_price``; bump exclusive floors."""
        if self.min_exclusive:
            return float(self.min_price) + 1e-12
        return float(self.min_price)

    @property
    def fak_limit(self) -> float:
        """Limit posted on the BUY FAK (band max unless ``limit_price`` is set)."""
        lim = float(self.limit_price or 0)
        return lim if lim > 0 else float(self.max_price)


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
    late_90_start_s: float = 0.0,
    late_90_min: float = 0.90,
    late_90_max: float = 0.99,
) -> Optional[EntryBand]:
    """Primary band for this TTM (late, then ≥90 early, then ≥95).

    Prefer ``applicable_entry_bands`` when more than one window can fire
    (first 3 min ≥90 and ≥95 overlap; last 45s 75–90 plus ≥90 overlay).
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
        late_90_start_s=late_90_start_s,
        late_90_min=late_90_min,
        late_90_max=late_90_max,
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
    late_90_start_s: float = 0.0,
    late_90_min: float = 0.90,
    late_90_max: float = 0.99,
) -> List[EntryBand]:
    """Every 5m entry band that is open at this TTM (may be more than one).

    Late: ``seconds_left <= late_start_s`` → inclusive 75–90¢
    (``late_max`` is ``buy_max_price``, typically 0.90). TTM 46–120 does
    **not** buy 91–99¢.

    Last 45s ≥90 overlay: ``seconds_left <= late_90_start_s`` → ask at least
    90¢ up to 99¢, FAK 99¢, still the late $2.50 slice. Exactly 90¢ stays
    on the late 90¢ FAK (``select_entry_band`` picks the lower retry floor).

    Early ≥90: ``late_start_s < seconds_left <= early_start_s`` → ask at least
    90¢ (first 3 minutes of a 5m market).

    Early ≥95: ``early_95_min_s <= seconds_left <= early_95_start_s`` **and**
    ``seconds_left > late_start_s`` → ask at least 95¢. It does not overlay
    the last 120s.
    """
    try:
        ttm = float(seconds_left)
        late_s = float(late_start_s)
        early_s = float(early_start_s)
        early_95_s = float(early_95_start_s)
        early_95_floor = float(early_95_min_s)
        late_90_s = float(late_90_start_s)
    except (TypeError, ValueError):
        return []
    if ttm <= 0:
        return []
    bands: List[EntryBand] = []
    if ttm <= late_s + 1e-12:
        bands.append(EntryBand(float(late_min), float(late_max), False, "late"))
    if late_90_s > 1e-12 and ttm <= late_90_s + 1e-12:
        bands.append(EntryBand(
            float(late_90_min), float(late_90_max), False, "late_90",
            float(late_90_max),
        ))
    if early_s > late_s + 1e-12 and late_s + 1e-12 < ttm <= early_s + 1e-12:
        bands.append(EntryBand(float(early_min), float(early_max), False, "early"))
    # ≥95 is an early-window overlay only. Last 120s stays late 75–90
    # except the last-45s ≥90 overlay above.
    if (
        early_95_s > early_95_floor + 1e-12
        and early_95_floor - 1e-12 <= ttm <= early_95_s + 1e-12
        and ttm > late_s + 1e-12
    ):
        bands.append(EntryBand(
            float(early_95_min), float(early_max), False, "early_95",
        ))
    return bands


def is_late_entry_window(seconds_left, late_start_s) -> bool:
    """True when TTM is inside the last-120s (late) slice."""
    try:
        ttm = float(seconds_left)
        late_s = float(late_start_s)
    except (TypeError, ValueError):
        return False
    return 0 < ttm <= late_s + 1e-12


def entry_slice_budget(
    seconds_left,
    *,
    late_start_s,
    early_budget,
    late_budget,
) -> float:
    """USDC for this TTM slice (early $2.50 or late $2.50), not the $5 total."""
    if is_late_entry_window(seconds_left, late_start_s):
        return float(late_budget)
    return float(early_budget)


def slice_bought_key(seconds_left, late_start_s) -> str:
    return "late_bought" if is_late_entry_window(seconds_left, late_start_s) else "early_bought"


def stamp_slice_bought(meta, late_slice):
    """Record that this TTM slice has used its $2.50 (one fill per slice)."""
    if late_slice:
        meta["late_bought"] = True
    else:
        meta["early_bought"] = True


def stamp_slice_on_inventory(meta, late_slice, filled):
    """Stamp a slice only when confirmed inventory exists.

    Ghosts / confirm timeouts must not consume early_bought / late_bought
    without shares (live 22 Aug: 61 buy_ghost_fill, 136 order_confirm_timeout).
    """
    try:
        size = float(filled or 0)
    except (TypeError, ValueError):
        return False
    if size <= 0.01:
        return False
    stamp_slice_bought(meta, late_slice)
    return True


FIVE_M_BAND_DEFAULTS = {
    "late_start_s": 120,
    "late_min": 0.75,
    "late_max": 0.90,
    "early_start_s": 300,
    "early_min": 0.90,
    "early_max": 0.99,
    "early_95_start_s": 300,
    "early_95_min_s": 60,
    "early_95_min": 0.95,
    "late_90_start_s": 45,
    "late_90_min": 0.90,
    "late_90_max": 0.99,
}


def decide_5m_entry(seconds_left, ask, **band_kwargs):
    """Band that may POST at this TTM + ask, or None (no POST).

    Late TTM (45, 120]: 75–90 only. 93¢ at TTM 116 or 60 is a no.
    Last 45s (0, 45]: 75–90 plus ≥90 overlay (93¢ POSTs as late_90 / FAK 99).
    Early TTM (120, 300]: ≥90. 85¢ at TTM 180 is a no.
    """
    kwargs = dict(FIVE_M_BAND_DEFAULTS)
    kwargs.update(band_kwargs)
    bands = applicable_entry_bands(seconds_left, **kwargs)
    return select_entry_band(ask, bands)


def validate_late_90_start_s(late_90_start_s, buy_start_s) -> float:
    """Reject a last-45 overlay that is negative or wider than the late window."""
    try:
        overlay = float(late_90_start_s)
        late = float(buy_start_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("late_90_start_s must be a number") from exc
    if overlay < 0:
        raise ValueError("late_90_start_s must be >= 0")
    if overlay > late + 1e-12:
        raise ValueError("late_90_start_s must be <= buy_start_s")
    return overlay


def late_90_window_ok(seconds_left, late_90_start_s=45, late_start_s=120) -> bool:
    """True when TTM is in (0, late_90_start_s] inside the late window."""
    try:
        overlay = validate_late_90_start_s(late_90_start_s, late_start_s)
        ttm = float(seconds_left)
    except (TypeError, ValueError):
        return False
    return overlay > 1e-12 and 0 < ttm <= overlay + 1e-12


def buy_retry_fak_limit(armed_max, live_band):
    """Pin FAK limit to the live open band (never walk 99 after late_90 closed).

    ``None`` → abort POST (no live band). Else ``min(armed, live fak)``.
    Armed late_90 @ 99 with a live 90¢ ask becomes a 90¢ FAK, not an abort
    and not a 99¢ walk through 91–99.
    """
    if live_band is None:
        return None
    try:
        armed = float(armed_max)
        live = float(live_band.fak_limit)
    except (TypeError, ValueError):
        return None
    if armed <= 0 or live <= 0:
        return None
    return min(armed, live)


def classify_fill_against_band(
    avg,
    band_min,
    band_max=None,
    toxic_below=0.65,
):
    """below/above the open band, and whether the fill is toxic.

    Average **outside** the band (late 93, early 85) dumps. In-band 87¢
    late is not toxic — that bag still dumps when held bid ≤ 53¢.
    """
    try:
        avg_f = float(avg)
    except (TypeError, ValueError):
        avg_f = 0.0
    try:
        lo = float(band_min or 0)
    except (TypeError, ValueError):
        lo = 0.0
    try:
        hi = float(band_max) if band_max is not None else None
    except (TypeError, ValueError):
        hi = None
    try:
        floor = float(toxic_below or 0)
    except (TypeError, ValueError):
        floor = 0.0
    below = lo > 0 and avg_f + 1e-9 < lo
    above = hi is not None and hi > 0 and avg_f > hi + 1e-9
    outside = below or above
    toxic = outside or (floor > 0 and avg_f + 1e-9 < floor)
    return below, above, toxic


def slice_already_filled(meta, seconds_left, late_start_s) -> bool:
    return bool((meta or {}).get(slice_bought_key(seconds_left, late_start_s)))


def same_token(held_token, buy_token) -> bool:
    """True when there is no held token yet, or both ids match."""
    if held_token in (None, "") or buy_token in (None, ""):
        return True
    return str(held_token) == str(buy_token)


def late_add_blocked_by_min(
    ask,
    *,
    held_size=0.0,
    hedge_closed=False,
    add_min_price=0.0,
) -> bool:
    """True when a same-leg add would buy a fade cheaper than ``add_min_price``.

    Flat first entries (no inventory) are never blocked. ``add_min_price``
    ≤ 0 disables the floor.
    """
    if bool(hedge_closed):
        return False
    if float(held_size or 0) <= 0.01:
        return False
    try:
        floor = float(add_min_price or 0)
    except (TypeError, ValueError):
        return False
    if floor <= 1e-12:
        return False
    if ask is None:
        return False
    try:
        return float(ask) < floor - 1e-12
    except (TypeError, ValueError):
        return True


def can_arm_entry_slice(
    meta,
    *,
    seconds_left,
    late_start_s,
    held_size=0.0,
    buy_token=None,
    hedge_closed=False,
    ask=None,
    add_min_price=0.0,
):
    """Whether this TTM slice may still POST.

    One fill per slice. A live bag on the other leg blocks a late add
    (no straddle). After a full hedge, this market is done — no other-leg
    chase. A same-leg late add cheaper than ``add_min_price`` is a fade.

    Returns ``(ok, skip_reason)``. ``skip_reason`` is None when ok.
    """
    meta = meta or {}
    if meta.get("buy_uncertain"):
        return False, "buy_uncertain"
    if slice_already_filled(meta, seconds_left, late_start_s):
        return False, "slice_filled"
    closed = bool(hedge_closed or meta.get("hedge_closed"))
    if closed:
        return False, "hedge_closed"
    late = is_late_entry_window(seconds_left, late_start_s)
    held = float(held_size or 0) > 0.01
    tracked = meta.get("bought_token")
    if not late:
        if meta.get("early_bought") or tracked:
            return False, "slice_filled"
        if held:
            return False, "already_held"
        return True, None
    if held and buy_token is not None and tracked and not same_token(tracked, buy_token):
        return False, "other_leg"
    if late_add_blocked_by_min(
        ask,
        held_size=held_size,
        hedge_closed=closed,
        add_min_price=add_min_price,
    ):
        return False, "add_below_min"
    return True, None


def accumulate_buy_inventory(
    prior_size,
    prior_cost,
    filled,
    spent,
    prior_quoted=0.0,
    this_quoted=0.0,
):
    """Combine an earlier slice with this FAK. Returns size, cost, quoted, vwap."""
    size = float(prior_size or 0) + float(filled or 0)
    cost = float(prior_cost or 0) + float(spent or 0)
    quoted = float(prior_quoted or 0) + float(this_quoted or 0)
    avg = (cost / size) if size > 1e-12 else 0.0
    return size, cost, quoted, avg


def uncertain_buy_spend_cap(
    known_cost,
    slice_budget,
    max_spend,
    slice_prior_cost=0.0,
) -> float:
    """Remaining USDC this BUY FAK may still credit.

    ``known_cost`` is total persisted entry cost (earlier slices + this
    trigger). ``slice_prior_cost`` is cost before this trigger. Remaining is
    ``min(slice_budget, max_spend)`` minus this trigger's already-persisted
    spend, so a late $2.50 add is not crushed by an early $2.50 fill.
    """
    cap = min(float(slice_budget), float(max_spend))
    this_spent = max(0.0, float(known_cost or 0) - float(slice_prior_cost or 0))
    return max(0.0, cap - this_spent)


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


# --- Hourly three slices (minutes TTM; do not feed 5m seconds into these) ---

HOURLY_SLICE_A = "a22"
HOURLY_SLICE_B = "b15"
HOURLY_SLICE_C = "c5"
HOURLY_SLICE_FLAGS = {
    HOURLY_SLICE_A: "t22_bought",
    HOURLY_SLICE_B: "t15_bought",
    HOURLY_SLICE_C: "t5_bought",
}
_HOURLY_SLICE_PRIORITY = {
    HOURLY_SLICE_B: 0,
    HOURLY_SLICE_C: 1,
    HOURLY_SLICE_A: 2,
}


def hourly_horizon_min(a22_window_min, b15_window_min, c5_window_min, buy_window_min=0.0) -> float:
    """Widest hourly look-ahead in minutes (slice A is 22)."""
    try:
        return max(
            float(a22_window_min),
            float(b15_window_min),
            float(c5_window_min),
            float(buy_window_min or 0),
        )
    except (TypeError, ValueError):
        return 0.0


def applicable_hourly_entry_bands(
    minutes_left,
    *,
    a22_window_min=22.0,
    b15_window_min=15.0,
    c5_window_min=5.0,
    b15_min=0.75,
    b15_max=0.90,
    a22_min=0.93,
    c5_min=0.95,
    high_max=0.99,
) -> List[EntryBand]:
    """Open hourly bands at this TTM (minutes). Inclusive ``0 < ttm <= window``.

    A window ``<= 0`` disables that slice. Live hourly is **B only**: last
    **20 min**, ask **75–90¢ inclusive**, FAK **90¢**. A (last 22, >93) and
    C (last 5, >95) stay in the helper for tests / re-enable.

    Slice A (last 22 min when ``a22_window_min > 0``): ask **> 0.93**. FAK
    limit **99¢**. Cap **$5**. When slice C is also open, A only matches
    **> 0.93 and ≤ 0.95** so a >95¢ print in the last 5 min uses C, not A.

    Slice B: ask **75–90¢ inclusive**. FAK limit **90¢**. Spend remaining to
    the $10 market cap.

    Slice C (when ``c5_window_min > 0``): ask **> 0.95**. FAK limit **99¢**.
    """
    try:
        ttm = float(minutes_left)
        a22_w = float(a22_window_min)
        b15_w = float(b15_window_min)
        c5_w = float(c5_window_min)
    except (TypeError, ValueError):
        return []
    if ttm <= 0:
        return []
    bands: List[EntryBand] = []
    # Window ≤ 0 disables that slice (live hourly: A/C off, B last 20 min 75–90).
    a22_open = a22_w > 0 and ttm <= a22_w + 1e-12
    b15_open = b15_w > 0 and ttm <= b15_w + 1e-12
    c5_open = c5_w > 0 and ttm <= c5_w + 1e-12
    if b15_open:
        bands.append(EntryBand(
            float(b15_min), float(b15_max), False, HOURLY_SLICE_B, float(b15_max),
        ))
    if a22_open:
        # When C is open, keep 0.95 in A (≤95 uses A) and leave >95 to C.
        a_max = float(c5_min) if c5_open else float(high_max)
        bands.append(EntryBand(
            float(a22_min), a_max, True, HOURLY_SLICE_A, float(high_max),
        ))
    if c5_open:
        bands.append(EntryBand(
            float(c5_min), float(high_max), True, HOURLY_SLICE_C, float(high_max),
        ))
    return bands


def select_hourly_entry_band(
    ask: Optional[float], bands: Sequence[EntryBand],
) -> Optional[EntryBand]:
    """Band that contains the ask. 75–90 is B; >95 in last 5 is C not A."""
    matching = [
        band for band in bands
        if ask_in_entry_band(
            ask, band.min_price, band.max_price,
            min_exclusive=band.min_exclusive,
        )
    ]
    if not matching:
        return None
    return min(
        matching,
        key=lambda band: (
            _HOURLY_SLICE_PRIORITY.get(band.name, 9),
            band.retry_min_price,
            -band.max_price,
        ),
    )


def hourly_spent_so_far(meta) -> float:
    """USDC already credited on this market (sum of fills)."""
    return float((meta or {}).get("pnl_entry_cost") or 0)


def hourly_remaining_to_cap(meta, market_cap=10.0) -> float:
    return max(0.0, float(market_cap) - hourly_spent_so_far(meta))


def hourly_slice_budget(
    slice_name,
    meta,
    *,
    a22_budget=5.0,
    b15_budget=10.0,
    c5_budget=10.0,
    market_cap=10.0,
) -> float:
    """USDC this slice may still POST.

    A is never more than ``a22_budget`` ($5) even if the $10 cap is unused.
    B and C spend remaining to ``market_cap`` ($10 if flat, $5 if $5 already in).
    """
    remaining = hourly_remaining_to_cap(meta, market_cap)
    if remaining < 0.01:
        return 0.0
    name = str(slice_name or "")
    if name == HOURLY_SLICE_A:
        return min(float(a22_budget), remaining)
    if name == HOURLY_SLICE_B:
        return min(float(b15_budget), remaining)
    if name == HOURLY_SLICE_C:
        return min(float(c5_budget), remaining)
    return 0.0


def stamp_hourly_slice_bought(meta, slice_name):
    """One fill per named hourly slice (``t22_bought`` / ``t15_bought`` / ``t5_bought``)."""
    key = HOURLY_SLICE_FLAGS.get(str(slice_name or ""))
    if key:
        meta[key] = True


def can_arm_hourly_slice(
    meta,
    *,
    slice_name,
    held_size=0.0,
    buy_token=None,
    hedge_closed=False,
    market_cap=10.0,
    a22_budget=5.0,
    b15_budget=10.0,
    c5_budget=10.0,
):
    """Whether this named hourly slice may still POST.

    One fill per slice. Same-leg add only. After a full hedge the market is
    permanently closed, including legacy records with no token/size/slice
    fields. ``buy_token=None`` skips the other-leg check (caller does not know
    the winner yet).

    Returns ``(ok, skip_reason)``.
    """
    meta = meta or {}
    closed = bool(hedge_closed or meta.get("hedge_closed"))
    if closed:
        return False, "hedge_closed"
    if meta.get("buy_uncertain"):
        return False, "buy_uncertain"
    name = str(slice_name or "")
    flag = HOURLY_SLICE_FLAGS.get(name)
    if flag and meta.get(flag):
        return False, "slice_filled"
    if hourly_slice_budget(
        name, meta,
        a22_budget=a22_budget,
        b15_budget=b15_budget,
        c5_budget=c5_budget,
        market_cap=market_cap,
    ) < 0.01:
        return False, "spend_cap"
    held = float(held_size or 0) > 0.01
    tracked = meta.get("bought_token")
    if held and buy_token is not None and tracked and not same_token(tracked, buy_token):
        return False, "other_leg"
    return True, None


def hourly_entry_final_gate(
    minutes_left,
    *,
    selected_slice,
    buy_ask,
    bands,
    buy_leg,
    oracle_check,
    oracle_gate_enabled=True,
    hedge_closed=False,
    buy_book_ok=False,
    buy_clob_winner=False,
    buy_gui=None,
    other_gui=None,
    min_winner_bid=0.70,
    max_loser_bid=0.30,
    min_gui_edge=0.0,
):
    """Final pure hourly BUY gate, evaluated immediately before every POST.

    The selected slice must still contain the live ask, the market must remain
    open and unhedged, the Binance/PTB gate must be enabled and fresh/favor
    the selected leg, and the selected leg must still be the CLOB/GUI winner.
    """
    try:
        ttm = float(minutes_left)
    except (TypeError, ValueError):
        return False, "invalid_ttm"
    if ttm <= 0:
        return False, "expired"
    if bool(hedge_closed):
        return False, "hedge_closed"

    live_band = select_hourly_entry_band(buy_ask, bands)
    if live_band is None or live_band.name != str(selected_slice or ""):
        return False, "band_closed"
    if not bool(buy_book_ok):
        return False, "book_not_winner"
    if not bool(buy_clob_winner):
        return False, "clob_side_flip"

    leg = str(buy_leg or "").strip().lower()
    if leg not in ("up", "down"):
        return False, "bad_leg"
    if not bool(oracle_gate_enabled):
        return False, "underlying_gate_disabled"
    if not isinstance(oracle_check, dict) or not oracle_check.get("ok"):
        return False, "oracle_unavailable"
    favored = str(oracle_check.get("favored") or "").strip().lower()
    if favored != leg:
        return False, "oracle_side_flip"

    try:
        selected_gui = float(buy_gui)
        opposing_gui = float(other_gui)
        winner_floor = float(min_winner_bid)
        loser_cap = float(max_loser_bid)
        edge_floor = max(0.0, float(min_gui_edge or 0))
    except (TypeError, ValueError):
        return False, "incomplete_gui"
    if not (0 <= selected_gui <= 1 and 0 <= opposing_gui <= 1):
        return False, "incomplete_gui"
    if selected_gui <= opposing_gui + 1e-12:
        return False, "clob_gui_side_flip"
    if selected_gui - opposing_gui + 1e-12 < edge_floor:
        return False, "ambiguous"
    if selected_gui + 1e-12 < winner_floor or opposing_gui > loser_cap + 1e-12:
        return False, "no_consensus"
    return True, "ok"


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
