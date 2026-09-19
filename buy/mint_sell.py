"""Pure mint-sell policy helpers (no CLOB posts, no mintbot import).

Loser dump: arm when a sized loser bid is at/under ``sell_threshold`` (~3¢)
and the opposite sized bid is at/over ``sell_opposite_min`` (~90¢). Persist
that book for ``sell_persist_s`` (~5s), then FAK 3¢ → 2¢ when the live sized
bid is at/over the floor; if the live bid is below the floor, FAK at that
live bid. Empty FAK keeps or re-arms the persist latch. Keep the winner
for redeem unless its sized bid reaches ``sell_winner_min`` (~99¢).

After the loser is sold, optional held-leg dump: if the remaining leg's sized
bid stays under ``sell_dump_below`` (~80¢) for ``sell_dump_persist_s`` (~5s),
live-bid FAK the held leg.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence, Tuple

DEFAULT_SELL_KNOBS = {
    "sell_enabled": False,
    "sell_threshold": 0.03,
    "sell_floor": 0.02,
    "sell_opposite_min": 0.90,
    "sell_persist_s": 5.0,
    "sell_cooldown_s": 3.0,
    "sell_winner_min": 0.999,
    "sell_winner_cheap_if_loser_le": 0.03,
    "sell_winner_min_cheap": 0.99,
    "sell_dump_enabled": True,
    "sell_dump_below": 0.80,
    "sell_dump_persist_s": 5.0,
    "sell_min_bid_size": 1.0,
}


def _decode_amount(raw: Any, expected: float) -> Optional[float]:
    """Human units vs fixed-six, using the offered share count as a prior."""
    if raw is None or raw == "":
        return None
    try:
        human = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(human) or human < 0:
        return None
    fixed = human / 1_000_000.0
    expected_f = float(expected or 0)
    raw_text = str(raw).strip().lower()
    if expected_f > 0:
        human_err = abs(human - expected_f) / expected_f
        fixed_err = abs(fixed - expected_f) / expected_f
        best, best_err = (
            (human, human_err) if human_err <= fixed_err else (fixed, fixed_err)
        )
        if best_err <= 0.5:
            return best
    if "." in raw_text or "e" in raw_text:
        return human
    return fixed if abs(human) >= 10_000 else human


def parse_sell_fill_shares(result: Any, offered_shares: float) -> float:
    """Shares sold from a CLOB v2 SELL POST.

    ``makingAmount`` / ``size_matched`` are the share leg. ``takingAmount`` is
    USDC received and must not be treated as a share count.
    """
    if not isinstance(result, dict):
        return 0.0
    offered = float(offered_shares or 0)
    for key in ("size_matched", "matched", "makingAmount", "making_amount"):
        value = _decode_amount(result.get(key), offered)
        if value is None or value <= 0:
            continue
        if offered > 0 and value > offered * 1.01 + 1e-6:
            continue
        return float(value)
    return 0.0


def inventory_latch(
    balance: Optional[float], *, tol: float, seen_inventory: bool
) -> str:
    """Classify token balance for sell attempts.

    A transient zero before any inventory was observed is *not* already-flat
    (mint may still be settling). Only latch flat after we have seen shares.
    """
    if balance is None:
        return "unknown"
    if float(balance) + 1e-9 >= float(tol):
        return "has_inventory"
    if seen_inventory:
        return "already_flat"
    return "await_inventory"


def classify_loser(
    up_bid: Optional[float],
    dn_bid: Optional[float],
    *,
    threshold: float,
    opposite_min: float,
) -> Tuple[Optional[str], str]:
    """Return ``(loser_leg, reason)`` from sized best bids."""
    up_cheap = up_bid is not None and float(up_bid) <= float(threshold) + 1e-12
    dn_cheap = dn_bid is not None and float(dn_bid) <= float(threshold) + 1e-12
    if up_cheap and dn_cheap:
        return None, "both_cheap"
    if up_cheap:
        if dn_bid is None or float(dn_bid) + 1e-12 < float(opposite_min):
            return None, "wick_unconfirmed"
        return "up", "loser"
    if dn_cheap:
        if up_bid is None or float(up_bid) + 1e-12 < float(opposite_min):
            return None, "wick_unconfirmed"
        return "dn", "loser"
    return None, "none"


def persist_ready(
    qualify: bool,
    *,
    now_s: float,
    armed_ts: Optional[float],
    persist_s: float,
) -> Tuple[bool, Optional[float], str]:
    """Arm while ``qualify`` holds; fire after ``persist_s`` seconds."""
    if not qualify:
        return False, None, "reset"
    persist = float(persist_s or 0)
    if persist <= 0:
        armed = float(armed_ts) if armed_ts is not None else float(now_s)
        return True, armed, "immediate"
    if armed_ts is None:
        return False, float(now_s), "armed"
    if float(now_s) + 1e-12 < float(armed_ts) + persist:
        return False, float(armed_ts), "waiting"
    return True, float(armed_ts), "ready"


def winner_cashout_leg(
    up_bid: Optional[float],
    dn_bid: Optional[float],
    winner_min: float,
) -> Optional[str]:
    """Leg whose sized bid is at/over the winner cash-out (not both)."""
    up_hit = up_bid is not None and float(up_bid) + 1e-12 >= float(winner_min)
    dn_hit = dn_bid is not None and float(dn_bid) + 1e-12 >= float(winner_min)
    if up_hit == dn_hit:
        return None
    return "up" if up_hit else "dn"


def empty_fak_status(status: Any) -> bool:
    """True when CLOB rejected a FAK because no resting bid matched."""
    return "no orders found" in str(status or "").lower()


def loser_persist_ready(
    qualify: bool,
    *,
    now_s: float,
    armed_ts: Optional[float],
    persist_s: float,
    last_status: Optional[str] = None,
    book_empty: bool = False,
) -> Tuple[bool, Optional[float], str]:
    """Like persist_ready; empty FAK must not drop the loser arm forever."""
    fire, armed, why = persist_ready(
        qualify, now_s=now_s, armed_ts=armed_ts, persist_s=persist_s
    )
    if why != "reset" or not book_empty or not empty_fak_status(last_status):
        return fire, armed, why
    if armed_ts is not None:
        return False, float(armed_ts), "empty_fak_keep_arm"
    return False, float(now_s), "empty_fak_rearm"


def loser_ladder_limits(
    threshold: float, floor: float, loser_bid: float
) -> Sequence[float]:
    """FAK limits: threshold→floor when bid ≥ floor; live bid when below floor."""
    thr = round(float(threshold), 4)
    fl = round(float(floor), 4)
    bid = round(float(loser_bid), 4)
    if bid + 1e-12 < fl:
        return [bid] if bid > 1e-12 else []
    limits: list[float] = []
    for limit in sorted({thr, fl}, reverse=True):
        use_px = round(max(fl, min(limit, bid)), 4)
        if not limits or abs(use_px - limits[-1]) > 1e-12:
            limits.append(use_px)
    return limits
