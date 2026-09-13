"""Pure 15m probe gates (no bot import, no I/O).

Used by tests and ``check_15m_probe_now.py``. ``buybot.py`` keeps its own
hot-reload loop; these helpers document the probe arithmetic so a later
$1–2 / $20 live cap is a JSON change, not a rewrite.
"""

from __future__ import annotations

from typing import Optional, Tuple

EPS = 1e-12


def in_buy_window(seconds_left: float, window_min: float) -> bool:
    """True when TTM is inside the last ``window_min`` minutes (exclusive of expiry)."""
    try:
        left = float(seconds_left)
        window_s = float(window_min) * 60.0
    except (TypeError, ValueError):
        return False
    if left != left or window_s != window_s:
        return False
    return 0 < left <= window_s + EPS


def ask_in_band(ask: Optional[float], min_price: float, max_price: float) -> bool:
    """True when the live ask sits in the inclusive FAK band (ge95 = 0.95–0.99)."""
    try:
        px = float(ask)  # type: ignore[arg-type]
        lo = float(min_price)
        hi = float(max_price)
    except (TypeError, ValueError):
        return False
    if px != px or lo != lo or hi != hi:
        return False
    return lo - EPS <= px <= hi + EPS


def probe_spend_usd(
    budget: float,
    max_spend: float,
    market_spend_cap: float = 0.0,
) -> float:
    """USDC this probe may send on one market.

    ``market_spend_cap`` 0 → ignore (``min(budget, max_spend)``). Probe uses
    $5 / $5 / $5. Later live $1–2 test or ~$20 cap changes these three numbers.
    """
    spend = min(float(budget), float(max_spend))
    cap = float(market_spend_cap or 0.0)
    if cap > 0:
        spend = min(spend, cap)
    return spend if spend == spend and spend > 0 else 0.0


def live_posting_armed(dry_run: bool, entry_enabled: bool) -> bool:
    """Real CLOB POSTs require both knobs off/on this way — never one alone."""
    return (not bool(dry_run)) and bool(entry_enabled)


def should_evaluate_entries(dry_run: bool, entry_enabled: bool) -> bool:
    """Dry-run still walks gates and logs; live with entries off does not."""
    return bool(dry_run) or bool(entry_enabled)


def entry_persist_key(condition_id: str, leg: str) -> str:
    return f"{condition_id}|{str(leg or '').strip().lower()}"


def shares_rail_needed(spend_usd: float, buy_threshold: float) -> float:
    """Minimum ``buy_max_shares`` so $spend at the band floor can size."""
    floor = float(buy_threshold)
    if floor <= 0:
        return 0.0
    return float(spend_usd) / floor


def probe_live_flip_note() -> Tuple[str, str]:
    """Exact later-live edit. Probe ships with both safeties on."""
    return (
        'Set `"dry_run": false` and restart polybuybot (dry_run is startup-locked).',
        'Then the one hot-reload knob is `"entry_enabled": true` (Joel only).',
    )
