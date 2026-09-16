"""Pure 15m probe gates (no bot import, no I/O).

Used by tests and ``check_15m_probe_now.py``. ``buybot.py`` keeps its own
hot-reload loop; these helpers document the probe arithmetic so a later
$1–2 / $20 live cap is a JSON change, not a rewrite.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from buy.hedge_gate import hedge_persist_ready

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


def entry_persist_key(condition_id: str, leg: str) -> str:
    return f"{condition_id}|{str(leg or '').strip().lower()}"


def persist_ask_ok(
    ask: Optional[float],
    persist_min: float = 0.95,
    persist_max: float = 0.99,
) -> bool:
    """Flash-filter band. Ask ≥ persist_min counts toward entry persist.

    Persist is not the buy floor. A 95¢ print starts the clock so a later
    96.5 / 97¢ buy (or a 95¢ buy) does not restart after the first touch.
    """
    return ask_in_band(ask, persist_min, persist_max)


def persist_leg_for_asks(
    *,
    up_winning: bool,
    dn_winning: bool,
    up_ask: Optional[float],
    dn_ask: Optional[float],
    persist_min: float = 0.95,
    persist_max: float = 0.99,
) -> Optional[str]:
    """Winning leg whose ask is in the persist band, else None.

    Consensus / oracle / buy_threshold are buy gates. They must not clear
    this arm — only leaving the persist band (or flipping side) does.
    """
    if up_winning and persist_ask_ok(up_ask, persist_min, persist_max):
        return "up"
    if dn_winning and persist_ask_ok(dn_ask, persist_min, persist_max):
        return "down"
    return None


def update_entry_persist(
    armed: Dict[str, float],
    *,
    condition_id: str,
    persist_leg: Optional[str],
    now_s: float,
    persist_s: float,
) -> Tuple[bool, str, float]:
    """Arm persist for ``persist_leg``; clear the other side.

    ``persist_leg`` None clears both. A 95→96→97 cascade keeps the same arm.
    Returns ``(ready, why, age_s)``.
    """
    for leg in ("up", "down"):
        if persist_leg != leg:
            armed.pop(entry_persist_key(condition_id, leg), None)
    if persist_leg is None:
        return False, "reset", 0.0
    key = entry_persist_key(condition_id, persist_leg)
    fire, new_armed, why = hedge_persist_ready(
        True,
        now_s=float(now_s),
        armed_ts=armed.get(key),
        persist_s=float(persist_s),
    )
    if new_armed is None:
        armed.pop(key, None)
        return False, why, 0.0
    armed[key] = float(new_armed)
    age = float(now_s) - float(new_armed)
    return bool(fire), why, age


def replay_entry_persist(
    ticks: Sequence[Tuple[float, Optional[float], Optional[float], bool, bool]],
    *,
    persist_s: float = 2.0,
    persist_min: float = 0.95,
    persist_max: float = 0.99,
    condition_id: str = "c",
) -> List[Tuple[bool, str, float, Optional[str]]]:
    """Replay persist through a cascade. Each tick is (t, up_ask, dn_ask, up_win, dn_win)."""
    armed: Dict[str, float] = {}
    out: List[Tuple[bool, str, float, Optional[str]]] = []
    for now_s, up_ask, dn_ask, up_win, dn_win in ticks:
        leg = persist_leg_for_asks(
            up_winning=up_win,
            dn_winning=dn_win,
            up_ask=up_ask,
            dn_ask=dn_ask,
            persist_min=persist_min,
            persist_max=persist_max,
        )
        ready, why, age = update_entry_persist(
            armed,
            condition_id=condition_id,
            persist_leg=leg,
            now_s=float(now_s),
            persist_s=persist_s,
        )
        out.append((ready, why, age, leg))
    return out


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
