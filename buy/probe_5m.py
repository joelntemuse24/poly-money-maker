"""Pure 5m probe gates (no bot import, no I/O).

Used by tests and ``check_5m_probe_now.py``. ``buybot5m.py`` keeps its own
hot-reload loop; these helpers document the probe arithmetic so a later
~$40 cap is a JSON change, not a rewrite.

Window is last ``buy_start_s`` seconds (probe: 90), not 15m's
``buy_window_min`` minutes. Band/spend/arm helpers are shared with 15m.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, Mapping, MutableMapping, Tuple

from buy.probe_15m import (  # noqa: F401 — re-export for 5m callers
    EPS,
    ask_in_band,
    live_posting_armed,
    persist_quote_ok,
    probe_spend_usd,
    shares_rail_needed,
    should_evaluate_entries,
)

CLOB_MIN_MARKETABLE_BUY_USDC = Decimal("1.00")
_SHARE_TICK = Decimal("0.01")
_CENT = Decimal("0.01")
_SPEND_KEYS = (
    "buy_budget",
    "late_buy_budget",
    "buy_max_spend",
    "market_spend_cap",
)


def min_marketable_buy_shares(limit: float, min_maker: float | None = None) -> float:
    """Smallest 2dp share size whose ``shares × limit`` is exact cents and ≥ $1.

    CLOB marketable BUY min is $1. At 99¢, 1.00 sh = $0.99 (400) and
    1.01 × 0.99 is not exact cents. Next legal size is 2.00 sh / $1.98.
    """
    try:
        lim = Decimal(str(limit))
    except (TypeError, ValueError, ArithmeticError):
        return 0.0
    if lim <= 0:
        return 0.0
    floor = (
        CLOB_MIN_MARKETABLE_BUY_USDC
        if min_maker is None
        else Decimal(str(min_maker))
    )
    if floor <= 0:
        floor = CLOB_MIN_MARKETABLE_BUY_USDC
    shares = (floor / lim).quantize(_SHARE_TICK, rounding=ROUND_UP)
    max_sh = Decimal("10000.00")
    while shares <= max_sh:
        maker = shares * lim
        if maker >= floor and maker.quantize(_CENT) == maker:
            return float(shares)
        shares = (shares + _SHARE_TICK).quantize(_SHARE_TICK, rounding=ROUND_DOWN)
    return 0.0


def min_marketable_buy_spend(limit: float) -> float:
    """USDC notional of ``min_marketable_buy_shares``. At 99¢ this is $1.98."""
    shares = min_marketable_buy_shares(limit)
    if shares < 0.01:
        return 0.0
    maker = Decimal(str(shares)) * Decimal(str(limit))
    return float(maker.quantize(_CENT))


def clob_min_slice_usd(limit: float) -> float:
    """Whole-dollar JSON floor that can post the min marketable BUY.

    $1.98 ceils to $2. Do not use $1.01: 1.01 × 0.99 is not exact cents.
    """
    need = Decimal(str(min_marketable_buy_spend(limit)))
    if need <= 0:
        return 0.0
    return float(need.quantize(Decimal("1"), rounding=ROUND_UP))


def raise_spend_for_clob_min_notional(cfg: MutableMapping[str, Any]) -> Dict[str, Mapping[str, float]]:
    """Bump 5m slice keys in memory so a 99¢ FAK can clear CLOB min $1.

    Does not write JSON. $5 probe / later ~$40 sizes are left alone.
    ``market_spend_cap`` 0 stays disabled (unlimited).
    """
    changed: Dict[str, Mapping[str, float]] = {}
    floor = clob_min_slice_usd(cfg.get("buy_max_price") or 0)
    if floor <= 0:
        return changed
    for key in _SPEND_KEYS:
        cur = float(cfg.get(key) or 0)
        if key == "market_spend_cap" and cur <= 0:
            continue
        if cur + EPS < floor:
            changed[key] = {"from": cur, "to": floor}
            cfg[key] = floor
    threshold = float(cfg.get("buy_threshold") or 0)
    if threshold > 0:
        need_sh = shares_rail_needed(
            max(float(cfg.get("buy_budget") or 0), float(cfg.get("late_buy_budget") or 0), floor),
            threshold,
        )
        cur_sh = float(cfg.get("buy_max_shares") or 0)
        if cur_sh + EPS < need_sh:
            bumped = float(Decimal(str(need_sh)).quantize(_SHARE_TICK, rounding=ROUND_UP))
            changed["buy_max_shares"] = {"from": cur_sh, "to": bumped}
            cfg["buy_max_shares"] = bumped
    return changed


def in_buy_window_s(seconds_left: float, window_s: float) -> bool:
    """True when TTM is inside the last ``window_s`` seconds (exclusive of expiry)."""
    try:
        left = float(seconds_left)
        window = float(window_s)
    except (TypeError, ValueError):
        return False
    if left != left or window != window:
        return False
    return 0 < left <= window + EPS


def probe_live_flip_note() -> Tuple[str, str]:
    """Exact later-live edit. Probe ships with both safeties on."""
    return (
        'Set `"dry_run": false` and restart polybuybot5m (dry_run is startup-locked).',
        'Then the one hot-reload knob is `"entry_enabled": true` (Joel only).',
    )
