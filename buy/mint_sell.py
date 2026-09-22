"""Pure mint-sell policy helpers (no CLOB posts, no mintbot import).

Loser dump: arm when a sized loser bid is at/under ``sell_threshold`` (~3¢)
and the opposite sized bid is at/over ``sell_opposite_min`` (~90¢). Persist
that book for ``sell_persist_s`` (~5s), or ``sell_persist_last_min_s`` (~2s)
when time-to-end is within ``sell_persist_last_min_window_s`` (~60s), then
re-check in-range at fire and FAK 3¢ → 2¢ when the live sized bid is at/over
the floor; if the live bid is below the floor, FAK at that live bid. Persist
waits fold typical ~4s sell-tick/FAK lag so wall-clock stays ~9s (last-min
~5–6s). Empty FAK, or a vanished loser book after arm, keeps ``armed_ts``
(do not fire until a sized bid at/under threshold returns). Keep the winner
for redeem unless its sized bid reaches ``sell_winner_min`` (~99¢).

In the last ``sell_late_window_s`` (~120s), also require a side-aware
Chainlink TWAP edge vs window open (≥ ``max(floor, per_ttm × TTM)`` for
``sell_oracle_edge_persist_s``) before firing the loser scrap. Outside that
window this module's CLOB gates are unchanged.

After the loser is sold, optional held-leg dump: if the remaining leg's sized
bid stays under ``sell_dump_below`` (~80¢) for ``sell_dump_persist_s`` (~2s),
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
    "sell_persist_last_min_s": 2.0,
    "sell_persist_last_min_window_s": 60.0,
    "sell_cooldown_s": 3.0,
    "sell_winner_min": 0.999,
    "sell_winner_cheap_if_loser_le": 0.03,
    "sell_winner_min_cheap": 0.99,
    "sell_clob_max_price": 0.99,
    "sell_clob_min_price": 0.01,
    "sell_dump_enabled": True,
    "sell_dump_below": 0.80,
    "sell_dump_persist_s": 2.0,
    # After first dump no-match/kill-0-fill, quickly refire this many times.
    "sell_dump_fak_retries": 2,
    # Dump retry ladder: top bid, then step down toward floor (short burst).
    "sell_dump_ladder_step": 0.04,
    "sell_dump_ladder_rungs": 4,
    "sell_min_bid_size": 1.0,
    # Cycle sleep while a loser persist arm is live. Does not change persist_s.
    "sell_armed_poll_s": 2.0,
    # Late-window oracle veto on full loser scrap (TTM ≤ window only).
    "sell_late_window_s": 120.0,
    "sell_oracle_edge_per_ttm": 1.5,
    "sell_oracle_edge_persist_s": 3.0,
    "sell_oracle_stale_s": 5.0,
    "sell_oracle_edge_floor_usd": 25.0,
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
    """True when the sell loop should keep ``sell_armed_poll_s`` cadence.

    Hot while the window is open and any of:
    - loser persist arm is live and the loser is not yet sold
    - loser is sold and dump/winner exit is not done (``sold_dump`` /
      ``sold_winner``)

    This is sell-loop scheduling only. Concurrent mint must not skip
    discovery because a bag is hot — that was the #193 serial-cycle
    bandage. Persist waits fold typical tick/FAK lag (defaults 5/2/60).

    Live audit (bag ``btc-updown-15m-1789880400``): ``poll_s=5`` plus
    manage_sells/reconcile/discover made sell ticks ≈8.6–10.5s. Persist
    ready was +9s; first post-ready look was empty_keep_arm at +10.7s.
    Incident ``btc-updown-15m-1789905600``: after ``loser_done``, dump
    stayed cold so next-window mint stole the shared cycle (~16s).
    """
    if not isinstance(intent, dict):
        return False
    if intent.get("status") not in _SELL_HOT_STATUSES:
        return False
    if not sell_window_open(now_s, float(intent.get("end_ts") or 0)):
        return False
    sold_exit = bool(intent.get("sold_dump") or intent.get("sold_winner"))
    if intent.get("sold_loser") or intent.get("sold_leg"):
        return not sold_exit
    return intent.get("sell_loser_armed_at") is not None


def skip_mint_discovery_for_sell(state: Any, now_s: float) -> bool:
    """True when any open intent is sell-hot (sell-loop cadence only).

    Name is historical. Concurrent mint no longer skips Gamma because
    this is true.
    """
    intents = (state or {}).get("intents") or {}
    if not isinstance(intents, dict):
        return False
    return any(sell_intent_hot(intent, now_s) for intent in intents.values())


def skip_mint_discovery_when_armed_and_capped(
    *,
    loser_armed: bool,
    mint_capped: bool,
) -> bool:
    """Serial-cycle bandage: skip Gamma only when armed *and* capped.

    Kept for tests of the old single-thread policy. Concurrent mint/sell
    loops must not call this — mint proceeds while sell stays hot.
    """
    return bool(loser_armed) and bool(mint_capped)


def mint_cycle_sleep_s(cfg: Any) -> float:
    """Mint/discover loop always uses ``poll_s``. Sell cadence is independent."""
    try:
        poll = float((cfg or {}).get("poll_s") or 10)
    except (TypeError, ValueError):
        poll = 10.0
    if poll < 0:
        return 0.0
    return poll


def cycle_sleep_s(cfg: Any, state: Any, now_s: float) -> float:
    """Sell-loop sleep: ``poll_s``, or ``sell_armed_poll_s`` while hot.

    Missing/invalid ``sell_armed_poll_s`` keeps ``poll_s`` so persist math is
    never replaced by the armed interval. Armed poll may be below the
    ``poll_s >= 2`` floor (live default 2.0). Mint uses
    ``mint_cycle_sleep_s`` and is not gated by this.
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
    tick so an arm started on the 5s clock can become ready on the 2s
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


def dump_fast_retry_eligible(*, sold: float, status: Any, tol: float) -> bool:
    """True when held-dump should immediately re-check and re-fire.

    The fast path only triggers for *zero-fill* misses:
    - CLOB explicit empty FAK (`no orders found`)
    - kill/cancel class statuses with zero fill
    """
    if float(sold or 0.0) + 1e-12 >= float(tol):
        return False
    text = str(status or "").lower()
    if empty_fak_status(status):
        return True
    return "kill" in text or "cancelled" in text or "canceled" in text


def dump_retry_ladder_limits(
    live_bid: float,
    *,
    floor: float,
    step: float,
    max_rungs: int,
) -> Sequence[float]:
    """Descending dump retry limits from live bid toward floor.

    The first retry level is always the current live sized bid. Additional
    levels descend in fixed ``step`` increments until floor (or rung cap).
    """
    bid = round(float(live_bid or 0.0), 4)
    if bid <= 1e-12:
        return []
    fl = round(float(floor or 0.0), 4)
    if bid + 1e-12 < fl:
        return [bid]
    rung_cap = max(1, int(max_rungs or 1))
    use_step = round(float(step or 0.0), 4)
    if use_step <= 1e-12:
        use_step = 0.01
    out = [bid]
    cur = bid
    while len(out) < rung_cap and cur > fl + 1e-12:
        cur = round(max(fl, cur - use_step), 4)
        if cur <= 1e-12:
            break
        if abs(cur - out[-1]) > 1e-12:
            out.append(cur)
    return out


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


def sell_fire_decision(
    path: str,
    *,
    bid: Optional[float],
    opposite_bid: Optional[float] = None,
    threshold: float = 0.03,
    floor: float = 0.02,
    opposite_min: float = 0.90,
    dump_below: float = 0.80,
    winner_min: float = 0.999,
    cheap_on: bool = False,
) -> Tuple[str, str]:
    """Last in-range check before a sell FAK.

    Returns ``("fire", reason)`` or
    ``("cancel_reset"|"cancel_keep_arm", reason)``. Persist-ready is not
    enough: if the live book left the path's range, do not POST. Empty loser
    books keep the arm; dump/winner empty books reset. Opposite-min and
    cheap-winner edge gates stay in force.
    """
    kind = str(path or "")
    if kind == "loser":
        if bid is None:
            return "cancel_keep_arm", "empty_book"
        if opposite_bid is None or float(opposite_bid) + 1e-12 < float(opposite_min):
            return "cancel_reset", "wick_unconfirmed"
        if float(bid) > float(threshold) + 1e-12:
            return "cancel_reset", "loser_above_threshold"
        if not loser_ladder_limits(threshold, floor, float(bid)):
            return "cancel_reset", "no_ladder_limit"
        return "fire", "loser"
    if kind == "dump":
        if bid is None:
            return "cancel_reset", "empty_book"
        if float(bid) + 1e-12 >= float(dump_below):
            return "cancel_reset", "dump_at_or_above_below"
        return "fire", "dump"
    if kind == "winner":
        if bid is None:
            return "cancel_reset", "empty_book"
        if float(bid) + 1e-12 < float(winner_min):
            return "cancel_reset", "winner_below_min"
        return "fire", "winner_cheap" if cheap_on else "winner"
    return "cancel_reset", "unknown_path"


def _finite_usd(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def side_aware_oracle_edge_usd(
    *,
    twap_usd: Any,
    open_usd: Any,
    scrap_leg: str,
) -> Optional[float]:
    """Side-aware TWAP edge vs window open that favors the *kept* leg.

    Scraping Down (keeping Up) needs ``twap - open >= 0`` in the caller's
    threshold check. Scraping Up (keeping Down) needs ``open - twap``.
    Returns signed edge in USD, or None when inputs are unusable.
    """
    twap = _finite_usd(twap_usd)
    open_px = _finite_usd(open_usd)
    if twap is None or open_px is None:
        return None
    leg = str(scrap_leg or "").lower()
    if leg in ("dn", "down"):
        return twap - open_px
    if leg in ("up",):
        return open_px - twap
    return None


def late_oracle_need_usd(
    ttm_s: float,
    *,
    edge_per_ttm: float = 1.5,
    floor_usd: float = 25.0,
) -> float:
    """Minimum side-aware edge required at this TTM (floor applied)."""
    ttm = max(0.0, float(ttm_s))
    need = float(edge_per_ttm) * ttm
    floor = float(floor_usd or 0.0)
    if floor > 0:
        return max(floor, need)
    return need


def late_oracle_scrap_ok(
    *,
    ttm_s: Optional[float],
    scrap_leg: Optional[str],
    twap_usd: Any,
    open_usd: Any,
    twap_age_s: Optional[float],
    late_window_s: float = 120.0,
    edge_per_ttm: float = 1.5,
    floor_usd: float = 25.0,
    stale_s: float = 5.0,
) -> Tuple[bool, str, dict]:
    """Whether the late-window oracle gate currently qualifies (one tick).

    Outside ``late_window_s`` returns ``(True, "outside_late_window", ...)``
    so callers skip the gate. Inside the window, fail closed on missing /
    stale / wrong-sign / thin edge. Does not apply the 3s persist arm —
    pair with ``persist_ready`` / ``late_oracle_edge_persist``.
    """
    detail: dict = {
        "ttm": None if ttm_s is None else float(ttm_s),
        "leg": scrap_leg,
        "twap": None,
        "open_usd": None,
        "edge": None,
        "need": None,
        "age_s": None if twap_age_s is None else float(twap_age_s),
    }
    if ttm_s is None:
        return False, "missing_ttm", detail
    ttm = float(ttm_s)
    detail["ttm"] = ttm
    window = float(late_window_s or 0.0)
    if window <= 0 or ttm > window + 1e-12:
        return True, "outside_late_window", detail
    if ttm <= 0:
        return False, "market_ended", detail
    twap = _finite_usd(twap_usd)
    open_px = _finite_usd(open_usd)
    detail["twap"] = twap
    detail["open_usd"] = open_px
    if open_px is None:
        return False, "missing_open", detail
    if twap is None:
        return False, "missing_twap", detail
    if twap_age_s is None:
        return False, "missing_twap_age", detail
    age = float(twap_age_s)
    detail["age_s"] = age
    if age > float(stale_s) + 1e-12:
        return False, "stale_twap", detail
    edge = side_aware_oracle_edge_usd(
        twap_usd=twap, open_usd=open_px, scrap_leg=str(scrap_leg or "")
    )
    detail["edge"] = edge
    if edge is None:
        return False, "bad_leg", detail
    need = late_oracle_need_usd(
        ttm, edge_per_ttm=edge_per_ttm, floor_usd=floor_usd
    )
    detail["need"] = need
    if edge + 1e-12 < need:
        return False, "edge_thin", detail
    return True, "edge_ok", detail


def late_oracle_edge_persist(
    qualify: bool,
    *,
    now_s: float,
    armed_ts: Optional[float],
    persist_s: float,
) -> Tuple[bool, Optional[float], str]:
    """3s (default) continuous edge arm for late loser scrap. Same as persist_ready."""
    return persist_ready(
        qualify, now_s=now_s, armed_ts=armed_ts, persist_s=persist_s
    )
