"""Pure sell/hedge decision logic — mirrors live bot, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Decision:
    action: str  # none | sell_up | sell_dn | hedge_up | hedge_dn
    reason: str
    limit_price: Optional[float] = None


def evaluate(
    *,
    seconds_left: float,
    up_bid: Optional[float],
    dn_bid: Optional[float],
    up_size: float,
    dn_size: float,
    sold_up: bool,
    sold_dn: bool,
    strategy: dict,
) -> Decision:
    """Return the action the live bot would take at this snapshot.

    Parameters match the live sell/hedge phase after books are known.
    """
    sell_window_s = float(strategy["sell_window_min"]) * 60.0
    thr = float(strategy["sell_threshold"])
    last_s = float(strategy["sell_lastchance_s"])
    last_thr = float(strategy["sell_lastchance_threshold"])
    hedge_on = bool(strategy["hedge_enabled"])
    hedge_thr = float(strategy["hedge_threshold"])

    if seconds_left <= 0:
        return Decision("none", "expired")

    # Hedge: only after one leg fully sold, dual-condition if we ever enable it.
    # Live bot at c8ea675: hedge if held leg bid <= hedge_threshold (when enabled).
    if hedge_on:
        if sold_up and not sold_dn and dn_size >= 0.01 and dn_bid is not None:
            if dn_bid <= hedge_thr:
                return Decision("hedge_dn", "hedge_held_leg", limit_price=max(0.01, dn_bid))
        if sold_dn and not sold_up and up_size >= 0.01 and up_bid is not None:
            if up_bid <= hedge_thr:
                return Decision("hedge_up", "hedge_held_leg", limit_price=max(0.01, up_bid))

    if seconds_left > sell_window_s:
        return Decision("none", "outside_sell_window")

    # Preserve remaining leg after a full sell of the other side
    if sold_dn and up_size >= 0.01 and dn_size < 0.01:
        # only UP left — do not sell it under normal threshold
        preserve_up = True
    else:
        preserve_up = False
    if sold_up and dn_size >= 0.01 and up_size < 0.01:
        preserve_dn = True
    else:
        preserve_dn = False

    up_trigger = bool(up_size > 0 and up_bid is not None and up_bid <= thr and not preserve_up)
    dn_trigger = bool(dn_size > 0 and dn_bid is not None and dn_bid <= thr and not preserve_dn)
    up_reason = "threshold" if up_trigger else None
    dn_reason = "threshold" if dn_trigger else None

    if seconds_left <= last_s and not up_trigger and not dn_trigger:
        confirm = 1.0 - last_thr
        up_cand = bool(up_size > 0 and up_bid is not None and up_bid < last_thr and not preserve_up)
        dn_cand = bool(dn_size > 0 and dn_bid is not None and dn_bid < last_thr and not preserve_dn)
        if up_cand and not dn_cand and dn_bid is not None and dn_bid >= confirm:
            up_trigger, up_reason = True, "last_chance"
        elif dn_cand and not up_cand and up_bid is not None and up_bid >= confirm:
            dn_trigger, dn_reason = True, "last_chance"
        elif up_cand or dn_cand:
            return Decision("none", "last_chance_unconfirmed")

    if up_trigger and dn_trigger:
        return Decision("none", "both_legs_triggered")

    if up_trigger:
        return Decision("sell_up", up_reason or "threshold", limit_price=up_bid)
    if dn_trigger:
        return Decision("sell_dn", dn_reason or "threshold", limit_price=dn_bid)
    return Decision("none", "no_trigger")
