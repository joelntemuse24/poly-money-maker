"""Pure mint-sell policy helpers (no CLOB posts, no mintbot import).

Loser dump: arm when a sized loser bid is at/under ``sell_threshold`` (~3¢)
and the opposite sized bid is at/over ``sell_opposite_min`` (~90¢). Persist
that book for ``sell_persist_s`` (~9s), or ``sell_persist_last_min_s`` (~5s)
when time-to-end is within ``sell_persist_last_min_window_s`` (~60s), then
FAK 3¢ → 2¢ when the live sized bid is at/over the floor; if the live bid
is below the floor, FAK at that live bid. Empty FAK, or a vanished loser
book after arm, keeps ``armed_ts`` (do not fire until a sized bid at/under
threshold returns). Keep the winner for redeem unless its sized bid reaches
``sell_winner_min`` (~99¢).

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
    "sell_persist_s": 9.0,
    "sell_persist_last_min_s": 5.0,
    "sell_persist_last_min_window_s": 60.0,
    "sell_cooldown_s": 3.0,
    "sell_winner_min": 0.999,
    "sell_winner_cheap_if_loser_le": 0.03,
    "sell_winner_min_cheap": 0.99,
    "sell_clob_max_price": 0.99,
    "sell_clob_min_price": 0.01,
    "sell_dump_enabled": True,
    "sell_dump_below": 0.80,
    "sell_dump_persist_s": 5.0,
    "sell_min_bid_size": 1.0,
    # Cycle sleep while sell is hot (loser arm through dump/winner exit).
    # Does not change persist_s.
    "sell_armed_poll_s": 2.0,
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


def sell_window_open(now_s: float, end_ts: float) -> bool:
    """CLOB sells run only while time-to-end is strictly positive."""
    if not end_ts:
        return True
    return float(end_ts) - float(now_s) > 0


_SELL_HOT_STATUSES = (
    "confirmed",
    "confirmed_waiting_inventory",
    "mined",
    "executed",
)


def sell_intent_hot(intent: Any, now_s: float) -> bool:
    """True while this intent still needs fast sell ticks in the window.

    Hot when the window is open and any of: loser persist arm is live and
    the loser is unsold; ``sold_loser`` / ``sold_leg`` and the held-leg dump
    or winner cash-out is not done (``sold_dump`` / ``sold_winner``); dump
    persist arm is live and dump is not done.

    Live audit (bag ``btc-updown-15m-1789880400``): ``poll_s=5`` plus
    manage_sells/reconcile/discover made sell ticks ≈8.6–10.5s. Persist
    ready is +9s; first post-ready look was empty_keep_arm at +10.7s.
    Miss = empty book on that look + coarse tick, not a stuck POST.
    Same-tick FAK already uses the fetched book. Faster sleep
    (``sell_armed_poll_s``, default 2s) is so a fleeting 2¢ bid between
    arms can be posted after persist, not to shorten persist.

    Bag ``btc-updown-15m-1789905600``: after ``sold_loser`` the old hot
    check returned False, sleep went back to ``poll_s=5``, and adjacent
    mint stole the cycle (loser_done 13:13:14 → first dump arm 13:13:30).
    Dump persist is 5s; stay hot until dump/winner exit so that gap dies.
    """
    if not isinstance(intent, dict):
        return False
    if intent.get("status") not in _SELL_HOT_STATUSES:
        return False
    if not sell_window_open(now_s, float(intent.get("end_ts") or 0)):
        return False
    sold_loser = bool(intent.get("sold_loser") or intent.get("sold_leg"))
    # manage_sells treats sold_winner as held-leg already exited (dump skip).
    sold_exit = bool(intent.get("sold_dump") or intent.get("sold_winner"))
    loser_armed = intent.get("sell_loser_armed_at") is not None
    dump_armed = intent.get("sell_dump_armed_at") is not None
    if loser_armed and not sold_loser:
        return True
    if sold_loser and not sold_exit:
        return True
    if dump_armed and not sold_exit:
        return True
    return False


def skip_mint_discovery_for_sell(state: Any, now_s: float) -> bool:
    """True when any open intent is still in the sell hot-poll window."""
    intents = (state or {}).get("intents") or {}
    if not isinstance(intents, dict):
        return False
    return any(sell_intent_hot(intent, now_s) for intent in intents.values())


def skip_mint_discovery_when_armed_and_capped(
    *,
    loser_armed: bool,
    mint_capped: bool,
) -> bool:
    """Defer Gamma/mint while sell is hot; ``mint_capped`` is unused.

    Adjacent mint used to stay available unless ``capped_open``. After
    ``sold_loser`` the slot is free, so that mint stole dump persist
    (bag ``btc-updown-15m-1789905600``). Hot poll skips discovery
    regardless of cap.
    """
    return bool(loser_armed)


def cycle_sleep_s(cfg: Any, state: Any, now_s: float) -> float:
    """``poll_s`` normally; ``min(poll_s, sell_armed_poll_s)`` while sell hot.

    Missing/invalid ``sell_armed_poll_s`` keeps ``poll_s`` so persist math is
    never replaced by the armed interval. Armed poll may be below the
    ``poll_s >= 2`` floor (live default 2.0).
    """
    poll = float((cfg or {}).get("poll_s") or 10)
    if poll < 0:
        poll = 0.0
    if not skip_mint_discovery_for_sell(state, now_s):
        return poll
    raw = (cfg or {}).get("sell_armed_poll_s")
    if raw is None or raw == "":
        return poll
    try:
        armed = float(raw)
    except (TypeError, ValueError):
        return poll
    if armed <= 0:
        return poll
    return min(poll, armed)


def effective_loser_persist_s(
    *,
    now_s: float,
    end_ts: float,
    persist_s: float,
    last_min_s: float,
    last_min_window_s: float,
) -> Optional[float]:
    """Loser persist for this tick, or None when the market has ended.

    Uses ``last_min_s`` when ``0 < end_ts - now_s <= last_min_window_s``.
    Callers pass this into ``persist_ready`` / ``loser_persist_ready`` each
    tick so an arm started on the 9s clock can become ready on the 5s
    clock without resetting ``armed_ts``.
    """
    if not end_ts:
        return float(persist_s)
    ttm = float(end_ts) - float(now_s)
    if ttm <= 0:
        return None
    window = float(last_min_window_s or 0)
    if window > 0 and ttm <= window + 1e-12:
        return float(last_min_s)
    return float(persist_s)


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


def winner_cheap_decision(
    sold_loser: bool,
    loser_fill: Optional[float],
    *,
    winner_min: float,
    cheap_gate: float,
    cheap_min: float,
) -> Tuple[float, bool, str]:
    """Return ``(effective_winner_min, cheap_enabled, reason)``.

    Cheap cash-out (typically 0.99) opens only when the loser is already sold
    at/under ``cheap_gate`` *and* ``loser_fill + cheap_min > 1.0`` (beats mint).
    Near-flat 1¢ + 99¢ keeps ``winner_min`` (0.999) so CLOB max 0.99 cannot fill
    and the winner waits for redeem.
    """
    base = float(winner_min)
    if not sold_loser:
        return base, False, "no_sold_loser"
    if loser_fill is None:
        return base, False, "no_loser_fill"
    try:
        loser = float(loser_fill)
    except (TypeError, ValueError):
        return base, False, "no_loser_fill"
    if not math.isfinite(loser):
        return base, False, "no_loser_fill"
    if loser > float(cheap_gate) + 1e-12:
        return base, False, "loser_above_cheap_gate"
    cheap = float(cheap_min)
    if round(loser + cheap, 4) <= 1.0:
        return base, False, "flat_or_negative_edge"
    return min(base, cheap), True, "positive_edge"


def winner_sell_limit(
    live_bid: float,
    *,
    clob_max: float = 0.99,
    clob_min: float = 0.01,
) -> Tuple[float, bool, str]:
    """Clamp live-bid winner FAK to a valid CLOB price.

    Resting books may quote 0.995–0.999; posting those limits is rejected
    (``invalid price (...), min: 0.01 - max: 0.99``). A sell FAK at
    ``clob_max`` still fills those richer bids.

    Returns ``(posted, clamped, reason)``. ``reason`` is ``clob_max`` or
    ``clob_min`` when the live bid is outside the CLOB range, else ``""``.
    """
    live = round(float(live_bid), 4)
    lo = round(float(clob_min), 4)
    hi = round(float(clob_max), 4)
    if live > hi + 1e-12:
        return hi, True, "clob_max"
    if live + 1e-12 < lo:
        return lo, True, "clob_min"
    return live, False, ""


def empty_fak_status(status: Any) -> bool:
    """True when CLOB rejected a FAK because no resting bid matched."""
    return "no orders found" in str(status or "").lower()


def loser_empty_keep_qualify(
    *,
    armed_ts: Optional[float],
    up_bid: Optional[float],
    dn_bid: Optional[float],
    opposite_min: float,
    prev_leg: Optional[str] = None,
    sold_loser: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Whether an existing loser arm should survive a missing loser book.

    Keep when already armed and the loser leg's sized bid is ``None``, as long
    as the opposite is still ≥ ``opposite_min`` *or* the opposite book is also
    empty (active arm, late book vanished). Full reset when the opposite is
    visibly below min, the loser bid is visible, never armed, or already sold.
    Returns ``(keep, loser_leg)``.
    """
    if armed_ts is None or sold_loser:
        return False, None
    bids = {"up": up_bid, "dn": dn_bid}
    leg: Optional[str] = prev_leg if prev_leg in ("up", "dn") else None
    if leg is None:
        if up_bid is None and dn_bid is None:
            return True, None
        if (
            up_bid is None
            and dn_bid is not None
            and float(dn_bid) + 1e-12 >= float(opposite_min)
        ):
            return True, "up"
        if (
            dn_bid is None
            and up_bid is not None
            and float(up_bid) + 1e-12 >= float(opposite_min)
        ):
            return True, "dn"
        return False, None
    loser_bid = bids[leg]
    opp_bid = bids["dn" if leg == "up" else "up"]
    if loser_bid is not None:
        return False, leg
    if opp_bid is not None and float(opp_bid) + 1e-12 < float(opposite_min):
        return False, leg
    return True, leg


def loser_persist_ready(
    qualify: bool,
    *,
    now_s: float,
    armed_ts: Optional[float],
    persist_s: float,
    last_status: Optional[str] = None,
    book_empty: bool = False,
) -> Tuple[bool, Optional[float], str]:
    """Like persist_ready; empty loser book / empty FAK must not drop the arm."""
    fire, armed, why = persist_ready(
        qualify, now_s=now_s, armed_ts=armed_ts, persist_s=persist_s
    )
    if why != "reset" or not book_empty:
        return fire, armed, why
    if armed_ts is not None:
        if empty_fak_status(last_status):
            return False, float(armed_ts), "empty_fak_keep_arm"
        return False, float(armed_ts), "empty_keep_arm"
    if empty_fak_status(last_status):
        return False, float(now_s), "empty_fak_rearm"
    return fire, armed, why


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
