"""Pure mint-sell policy helpers (no CLOB posts, no mintbot import).

Loser dump: arm when a sized loser bid is at/under ``sell_threshold`` (~2¢)
and the opposite sized bid is at/over ``sell_opposite_min`` (~90¢). Persist
that book for ``sell_persist_s`` (~5s), or ``sell_persist_last_min_s`` (~2s)
when time-to-end is within ``sell_persist_last_min_window_s`` (~60s). That
last-minute wait applies through market close. Sized depth does not skip
(``sell_persist_skip_when_sized`` default false). ``sell_scrap_max_ttm_s``
(code default 0, off) blocks arm, persist, and fire while seconds-to-close
is above the cutoff; the example sets 600. Unknown time-to-end leaves
that gate open. A bid that was already cheap still waits the full persist
once the gate opens. Then re-check
in-range at fire and FAK ``sell_fak_px`` (~2¢). That rung equals
``sell_floor`` (~2¢) when the live sized bid is at/over the floor; if the
live bid is below the floor, FAK at that live bid. ``sell_scrap_fraction``
defaults to 1 and scraps the whole loser. Below 1, the first fire locks
``floor(held × fraction)`` as the scrap target and leaves the remainder
unsold. Empty FAK, or a vanished
loser book after arm, keeps ``armed_ts``. On an empty keep, fire a blind
1¢ FAK (backoff ``sell_scrap_blind_backoff_s``). After a FAK miss, rest a
GTD/GTC sell. The price is ``sell_scrap_rest_px`` (~2¢, the print) capped
at the live or last-seen loser bid, so a 1¢ book is not posted at 2¢.
GTD only when expiration is at least ``sell_scrap_rest_min_ahead_s``
(~180s, Polymarket's floor) ahead; otherwise GTC.
Keep the winner for redeem unless its sized bid reaches ``sell_winner_min``
(~99¢).

``sell_late_window_s`` defaults to 0, which skips the Chainlink TWAP veto
on new loser posts. ``sell_oracle_edge_floor_usd``,
``sell_oracle_edge_per_ttm``, and ``sell_oracle_stale_s`` also default to
0, so a positive window does not restore the old dollar / stale
thresholds. ``sell_oracle_edge_persist_s`` stays 3s. The tape stays on.

After the loser is sold, optional held-leg dump: if the remaining leg's sized
bid stays under ``sell_dump_below`` (~80¢) for ``sell_dump_persist_s`` (~2s),
live-bid FAK the held leg. ``sell_dump_max_ttm_s`` (code default 0, off)
blocks that arm and fire while seconds-to-close is above the cutoff; the
example sets 240. A sister miss does not dump that leg. Wallet B
reads ``sell_dump_leg`` after this fill and buys the other side.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence, Tuple

DEFAULT_SELL_KNOBS = {
    "sell_enabled": False,
    "sell_threshold": 0.02,
    # Arm ceiling is sell_threshold. The scrap print is this rung (~2¢),
    # equal to the floor, or the live bid when the book is thinner.
    "sell_fak_px": 0.02,
    "sell_floor": 0.02,
    # One FAK at sell_floor for the full remainder. False restores the cent ladder.
    "sell_scrap_sweep_enabled": True,
    "sell_opposite_min": 0.90,
    "sell_persist_s": 5.0,
    "sell_persist_last_min_s": 2.0,
    "sell_persist_last_min_window_s": 60.0,
    # Off: full persist always. On: skip when depth at the FAK rung covers us.
    "sell_persist_skip_when_sized": False,
    "sell_scrap_blind_enabled": True,
    "sell_scrap_blind_px": 0.01,
    "sell_scrap_blind_backoff_s": 3.0,
    "sell_scrap_rest_enabled": True,
    # Post-miss resting sell ceiling. The posted rest is this, or the
    # live/last-seen bid when that bid is lower.
    "sell_scrap_rest_px": 0.02,
    # GTD expiration must be at least this far ahead; otherwise rest GTC.
    # Polymarket rejects a GTD inside ~180s.
    "sell_scrap_rest_min_ahead_s": 180.0,
    # 0 disables the scrap time-left gate (old behavior). The example sets 600.
    # Unknown ttm leaves the gate open. Dump uses sell_dump_max_ttm_s instead.
    "sell_scrap_max_ttm_s": 0.0,
    # 1 scraps the whole loser. Below 1, floor(held × fraction) is the
    # scrap target and the remainder is held to resolution.
    "sell_scrap_fraction": 1.0,
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
    # 0 disables the time-left gate (old behavior). The example sets 240.
    "sell_dump_max_ttm_s": 0.0,
    # When the held dump fills, also sell the kept scrap half (1c floor sweep).
    "sell_dump_also_kept": False,
    "sell_min_bid_size": 1.0,
    # Cycle sleep while a loser persist arm is live. Does not change persist_s.
    "sell_armed_poll_s": 2.0,
    # 0 skips the late-window Chainlink veto on loser scrap.
    # Floor, per-TTM, and stale are 0 so a positive window does not
    # restore the old veto. Edge persist stays 3s.
    "sell_late_window_s": 0.0,
    "sell_oracle_edge_per_ttm": 0.0,
    "sell_oracle_edge_persist_s": 3.0,
    "sell_oracle_stale_s": 0.0,
    "sell_oracle_edge_floor_usd": 0.0,
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
    ``poll_s >= 1`` floor (code default 2.0). Mint uses
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


def dump_time_gate_open(
    ttm_s: Optional[float],
    max_ttm_s: Optional[float],
) -> bool:
    """True when the held dump may arm or fire.

    ``max_ttm_s`` <= 0, missing, or non-finite leaves the gate off, so the
    dump ignores time-to-close. Otherwise seconds-to-close must be finite
    and at or under the cutoff. Ladder rungs after a dump has already
    fired are not this function's job.
    """
    try:
        cutoff = 0.0 if max_ttm_s is None else float(max_ttm_s)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(cutoff) or cutoff <= 0:
        return True
    if ttm_s is None:
        return False
    try:
        ttm = float(ttm_s)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(ttm):
        return False
    return ttm <= cutoff + 1e-12


def scrap_time_gate_open(
    ttm_s: Optional[float],
    max_ttm_s: Optional[float],
) -> bool:
    """True when loser scrap may arm, persist, or fire.

    ``max_ttm_s`` <= 0, missing, or non-finite leaves the gate off, so scrap
    ignores time-to-close. Unknown time-to-end (``None`` or non-finite)
    stays open: a missing clock does not block the scrap. Otherwise
    seconds-to-close must be at or under the cutoff. Same shape as
    ``dump_time_gate_open``, except that function treats a missing clock
    as closed.
    """
    try:
        cutoff = 0.0 if max_ttm_s is None else float(max_ttm_s)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(cutoff) or cutoff <= 0:
        return True
    if ttm_s is None:
        return True
    try:
        ttm = float(ttm_s)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(ttm):
        return True
    return ttm <= cutoff + 1e-12


def fresh_bag_risk() -> dict:
    """In-memory per-bag risk counters. Not persisted and not a trade input."""
    return {
        "partial": False,
        "scrap_seen": False,
        "scrapped_leg": None,
        "ttm_at_scrap": None,
        "scrap_avg_px": None,
        "loser_best_bid_size": None,
        "min_held_bid": None,
        "min_held_ttm": None,
        "sec_below_80": 0.0,
        "sec_below_65": 0.0,
        "sec_below_50": 0.0,
        "shortfall_weighted": 0.0,
        "shortfall_seconds": 0.0,
        "last_sample_ts": None,
        "last_held_bid": None,
        "dump_seen": False,
        "dump_fired": False,
        "dump_ttm": None,
        "dump_px": None,
    }


def _risk_num(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def bag_risk_add_span(rec: dict, *, now_s: float) -> None:
    """Add the open sample's tick delta, using the bid already on ``rec``."""
    last_ts = _risk_num(rec.get("last_sample_ts"))
    last_bid = _risk_num(rec.get("last_held_bid"))
    now = _risk_num(now_s)
    if last_ts is None or last_bid is None or now is None:
        return
    dt = now - last_ts
    if dt <= 0 or not math.isfinite(dt):
        return
    if last_bid < 0.80:
        rec["sec_below_80"] = float(rec.get("sec_below_80") or 0) + dt
    if last_bid < 0.65:
        rec["sec_below_65"] = float(rec.get("sec_below_65") or 0) + dt
    if last_bid < 0.50:
        rec["sec_below_50"] = float(rec.get("sec_below_50") or 0) + dt
    rec["shortfall_weighted"] = float(rec.get("shortfall_weighted") or 0) + (
        (1.0 - last_bid) * dt
    )
    rec["shortfall_seconds"] = float(rec.get("shortfall_seconds") or 0) + dt


def bag_risk_observe(
    rec: dict,
    *,
    now_s: float,
    ttm_s: Optional[float],
    sold_loser: bool,
    sold_leg: Optional[str],
    scrap_avg_px: Any = None,
    loser_best_bid_size: Any = None,
    scrap_ttm: Any = None,
    held_bid: Any = None,
    dump_fired: bool = False,
    dump_px: Any = None,
    dump_ttm: Any = None,
) -> None:
    """Update post-scrap held-bid stats. No orders, no intent writes."""
    if sold_loser and not rec.get("scrap_seen"):
        rec["scrap_seen"] = True
        rec["scrapped_leg"] = sold_leg if sold_leg in ("up", "dn") else None
        use_ttm = scrap_ttm if scrap_ttm is not None else ttm_s
        rec["ttm_at_scrap"] = _risk_num(use_ttm)
        rec["scrap_avg_px"] = _risk_num(scrap_avg_px)
        rec["loser_best_bid_size"] = _risk_num(loser_best_bid_size)
    if dump_fired and not rec.get("dump_seen"):
        rec["dump_seen"] = True
        rec["dump_fired"] = True
        use_dump_ttm = dump_ttm if dump_ttm is not None else ttm_s
        rec["dump_ttm"] = _risk_num(use_dump_ttm)
        rec["dump_px"] = _risk_num(dump_px)
    if not rec.get("scrap_seen"):
        return
    bid = _risk_num(held_bid)
    if bid is None:
        return
    bag_risk_add_span(rec, now_s=now_s)
    prev_min = _risk_num(rec.get("min_held_bid"))
    if prev_min is None or bid < prev_min - 1e-12:
        rec["min_held_bid"] = bid
        rec["min_held_ttm"] = _risk_num(ttm_s)
    rec["last_sample_ts"] = float(now_s)
    rec["last_held_bid"] = bid


def bag_risk_flush(rec: dict, *, now_s: float) -> None:
    """Fold the last held-bid sample through ``now_s`` once.

    Clears the open sample so a retried close does not add the span again.
    """
    if _risk_num(rec.get("last_held_bid")) is None:
        return
    bag_risk_add_span(rec, now_s=now_s)
    rec["last_sample_ts"] = None
    rec["last_held_bid"] = None


def bag_risk_payload(
    rec: dict,
    *,
    condition_id: str,
    slug: Any,
    shares: Any,
) -> dict:
    """One ``bag_risk`` log body. Nulls where the bag never reached that fact."""
    scrapped = bool(rec.get("scrap_seen"))
    seconds = float(rec.get("shortfall_seconds") or 0)
    mean = None
    if scrapped and seconds > 1e-12:
        mean = float(rec.get("shortfall_weighted") or 0) / seconds

    def rounded(value: Any, ndigits: int) -> Optional[float]:
        num = _risk_num(value)
        if num is None:
            return None
        return round(num, ndigits)

    dump_fired = bool(rec.get("dump_fired"))
    return {
        "condition_id": condition_id,
        "slug": slug,
        "shares": rounded(shares, 4),
        "scrapped_leg": rec.get("scrapped_leg") if scrapped else None,
        "ttm_at_scrap": rounded(rec.get("ttm_at_scrap"), 3) if scrapped else None,
        "scrap_avg_px": rounded(rec.get("scrap_avg_px"), 4) if scrapped else None,
        "loser_best_bid_size": (
            rounded(rec.get("loser_best_bid_size"), 4) if scrapped else None
        ),
        "min_held_bid": rounded(rec.get("min_held_bid"), 4) if scrapped else None,
        "min_held_ttm": rounded(rec.get("min_held_ttm"), 3) if scrapped else None,
        "sec_below_80": (
            round(float(rec.get("sec_below_80") or 0), 3) if scrapped else None
        ),
        "sec_below_65": (
            round(float(rec.get("sec_below_65") or 0), 3) if scrapped else None
        ),
        "sec_below_50": (
            round(float(rec.get("sec_below_50") or 0), 3) if scrapped else None
        ),
        "mean_shortfall": rounded(mean, 4),
        "dump_fired": dump_fired,
        "dump_ttm": rounded(rec.get("dump_ttm"), 3) if dump_fired else None,
        "dump_px": rounded(rec.get("dump_px"), 4) if dump_fired else None,
        "partial": bool(rec.get("partial")),
    }


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
    threshold: float,
    floor: float,
    loser_bid: float,
    *,
    fak_px: Optional[float] = None,
) -> Sequence[float]:
    """FAK limits. The arm ceiling is not the print.

    Top rung is ``fak_px`` when given, otherwise ``threshold`` for older
    callers. One fire walks every 1¢ from ``min(top, live bid)`` down to
    the floor, inclusive. Each rung is clamped to the live bid; duplicates
    are skipped. A live bid below the floor is the only rung. Defaults post
    a single 2¢ rung (``fak_px`` equals ``sell_floor``). A thinner book
    starts at the live bid.
    """
    tick = 0.01
    top = round(float(threshold if fak_px is None else fak_px), 4)
    fl = round(float(floor), 4)
    bid = round(float(loser_bid), 4)
    if bid + 1e-12 < fl:
        return [bid] if bid > 1e-12 else []
    limits: list[float] = []
    px = round(min(top, bid), 4)
    while True:
        use_px = round(max(fl, min(px, bid)), 4)
        if not limits or abs(use_px - limits[-1]) > 1e-12:
            limits.append(use_px)
        if use_px <= fl + 1e-12:
            break
        nxt = round(px - tick, 4)
        if nxt + 1e-12 >= px:
            break
        px = fl if nxt + 1e-12 < fl else nxt
    return limits


def sell_fire_decision(
    path: str,
    *,
    bid: Optional[float],
    opposite_bid: Optional[float] = None,
    threshold: float = 0.04,
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

    ``late_window_s`` <= 0 (the strategy default) returns
    ``(True, "outside_late_window", ...)`` and skips the gate, including
    a missing or stale tape. A positive window does the same when TTM is
    outside it. Inside a positive window, fail closed on missing / stale /
    wrong-sign / thin edge. Does not apply the 3s persist arm — pair with
    ``persist_ready`` / ``late_oracle_edge_persist``. The function default
    of 120s / $1.5 per second / $25 floor is the historical formula used
    when a caller omits those arguments. Strategy defaults pass 0 for the
    window, the dollar floor, the per-TTM slope, and the stale limit.
    ``sell_oracle_edge_persist_s`` stays 3 and is applied by the caller.
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


def advance_oracle_edge_arm(
    *,
    edge_ok: bool,
    sold_loser: bool,
    scrap_leg: Optional[str],
    now_s: float,
    armed_ts: Optional[float],
    armed_leg: Optional[str],
    persist_s: float,
    in_late: bool,
) -> Tuple[bool, Optional[float], str, Optional[str]]:
    """Advance the late-oracle persist clock for one sell tick.

    A missing loser bid does not reset an arm that already started: pass
    the kept scrap leg (``sell_loser_leg``) while the book is empty.
    The clock still resets when the edge fails, the bag is sold, the
    market leaves the late window, the scrap leg is unknown, or the
    scrap leg changes (opposite side).

    Returns ``(fire, armed_ts, why, armed_leg)``.
    """
    if not in_late or sold_loser or scrap_leg not in ("up", "dn"):
        return False, None, "reset", None
    if armed_leg in ("up", "dn") and armed_leg != scrap_leg:
        armed_ts = None
    fire, armed, why = late_oracle_edge_persist(
        bool(edge_ok),
        now_s=now_s,
        armed_ts=armed_ts,
        persist_s=persist_s,
    )
    return fire, armed, why, (scrap_leg if armed is not None else None)


def depth_covers_size(depth_at_limit: Optional[float], our_size: float) -> bool:
    """True when displayed bid depth at the FAK limit covers our shares."""
    try:
        depth = float(depth_at_limit)  # type: ignore[arg-type]
        need = float(our_size)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(depth) or not math.isfinite(need) or need <= 1e-12:
        return False
    return depth + 1e-9 >= need


def normalize_scrap_fraction(value: Any) -> float:
    """Strategy fraction in ``(0, 1]``. Missing or unusable values are 1."""
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(fraction) or fraction <= 0.0 or fraction > 1.0:
        return 1.0
    return fraction


def scrap_share_plan(held: float, fraction: float) -> Tuple[float, float]:
    """``(target, keep)`` shares for one loser scrap.

    Fraction 1 (the default) returns ``(held, 0)`` with no floor, so a
    full scrap still posts a fractional balance. Below 1, ``target`` is
    ``floor(held × fraction)`` whole shares and ``keep`` is the rest.
    """
    try:
        held_f = float(held)
    except (TypeError, ValueError):
        held_f = 0.0
    if not math.isfinite(held_f) or held_f < 0:
        held_f = 0.0
    frac = normalize_scrap_fraction(fraction)
    if frac >= 1.0 - 1e-12:
        return held_f, 0.0
    # 1e-9 keeps a binary value that is a hair under an integer from
    # flooring to the next share down. 100 × 0.5 stays 50.
    target = float(math.floor(held_f * frac + 1e-9))
    if target > held_f:
        target = held_f
    if target < 0:
        target = 0.0
    return target, held_f - target


def scrap_order_shares(
    *,
    target: float,
    keep: float,
    filled: float,
    inventory: float,
) -> float:
    """Next loser-scrap size.

    A zero keep (fraction 1) returns ``inventory`` unchanged. Otherwise
    the size is ``target - filled``, and never more than ``inventory - keep``.
    """
    try:
        inv = float(inventory)
    except (TypeError, ValueError):
        inv = 0.0
    if not math.isfinite(inv) or inv < 0:
        inv = 0.0
    try:
        keep_f = float(keep)
    except (TypeError, ValueError):
        keep_f = 0.0
    if not math.isfinite(keep_f) or keep_f <= 1e-12:
        return inv
    try:
        target_f = float(target)
        filled_f = float(filled or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(target_f):
        target_f = 0.0
    if not math.isfinite(filled_f):
        filled_f = 0.0
    remaining = max(0.0, target_f - filled_f)
    room = max(0.0, inv - keep_f)
    return min(remaining, room)


def scrap_target_met(
    *,
    filled: float,
    target: float,
    keep: float,
    tol: float,
    balance: Optional[float] = None,
) -> Tuple[bool, str]:
    """Whether the scrap target is done and the keep must stay put.

    Met when filled shares are within ``tol`` of ``target``, or a known
    balance is at or under ``keep + tol``.
    """
    try:
        tol_f = float(tol or 0.0)
        filled_f = float(filled or 0.0)
        target_f = float(target or 0.0)
        keep_f = float(keep or 0.0)
    except (TypeError, ValueError):
        return False, ""
    if not math.isfinite(tol_f) or tol_f < 0:
        tol_f = 0.0
    if not math.isfinite(filled_f):
        filled_f = 0.0
    if not math.isfinite(target_f):
        target_f = 0.0
    if not math.isfinite(keep_f) or keep_f < 0:
        keep_f = 0.0
    if filled_f + 1e-12 >= target_f - tol_f:
        return True, "target_filled"
    bal: Optional[float]
    if balance is None:
        bal = None
    else:
        try:
            bal = float(balance)
        except (TypeError, ValueError):
            bal = None
        if bal is not None and not math.isfinite(bal):
            bal = None
    if bal is not None and bal <= keep_f + tol_f + 1e-12:
        return True, "balance_at_keep" if keep_f > 1e-12 else "flat"
    return False, ""


def kept_leg_below_winner_min(
    leg: Optional[str],
    bid: Optional[float],
    *,
    sold_leg: Optional[str],
    keep: float,
    winner_min: float,
) -> bool:
    """True when ``leg`` is the kept loser and its bid is under ``winner_min``.

    The cheap 0.99 winner path stays on the other leg. Kept shares cash
    out only at the base winner minimum.
    """
    try:
        keep_f = float(keep or 0.0)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(keep_f) or keep_f <= 1e-12:
        return False
    if leg not in ("up", "dn") or sold_leg != leg:
        return False
    if bid is None:
        return True
    try:
        px = float(bid)
        floor = float(winner_min)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(px) or not math.isfinite(floor):
        return True
    return px + 1e-12 < floor


def loser_partial_fak_shares(
    *, remaining: float, depth_at_limit: Optional[float]
) -> float:
    """Clip a loser FAK to displayed depth when the book is short but present.

    Empty depth returns ``remaining`` so a blind FAK can still post the
    full remainder. A book that covers us returns ``remaining`` unchanged.
    """
    rem = max(0.0, float(remaining or 0))
    try:
        depth = float(depth_at_limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return rem
    if not math.isfinite(depth) or depth <= 1e-12:
        return rem
    return min(rem, depth)


def loser_scrap_post(
    *,
    sweep: bool,
    remaining: float,
    floor: float,
    threshold: float,
    loser_bid: float,
    fak_px: Optional[float] = None,
    depth_at_limit: Optional[float] = None,
) -> dict:
    """Loser scrap order for this fire.

    Sweep posts one FAK at ``floor`` for the full remainder. The book still
    fills best bids first. Flag off keeps the 1¢ ladder and the top-rung
    depth clip.
    """
    rem = max(0.0, float(remaining or 0.0))
    if sweep:
        return {
            "mode": "sweep",
            "limits": [round(float(floor), 4)],
            "size": rem,
        }
    limits = list(
        loser_ladder_limits(threshold, floor, loser_bid, fak_px=fak_px)
    )
    return {
        "mode": "ladder",
        "limits": limits,
        "size": loser_partial_fak_shares(
            remaining=rem, depth_at_limit=depth_at_limit
        ),
    }


def sell_fill_vwap(result: Any, sold_shares: float) -> Optional[float]:
    """USDC per share from a SELL fill. ``takingAmount`` is collateral."""
    try:
        sold = float(sold_shares or 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sold) or sold <= 1e-12 or not isinstance(result, dict):
        return None
    usdc = None
    for key in ("takingAmount", "taking_amount"):
        parsed = _decode_amount(result.get(key), 0.0)
        if parsed is not None and parsed > 0:
            usdc = parsed
            break
    if usdc is None:
        return None
    px = usdc / sold
    if not math.isfinite(px) or px <= 0 or px >= 1:
        return None
    return round(px, 4)


def record_fill_px(intent: dict, key: str, fills: Any) -> Optional[float]:
    """Fold ``(shares, px)`` fills into a running share-weighted average.

    ``intent[key]`` is the average price over every priced fill so far and
    ``intent[key + "_shares"]`` the shares it covers. Fills with no price
    (no ``takingAmount`` in the reply) are skipped rather than guessed, so
    readers fall back to the posted limit only when nothing was priced.
    """
    try:
        prev_px = intent.get(key)
        prev_sh = float(intent.get(key + "_shares") or 0.0)
        usd = float(prev_px) * prev_sh if prev_px is not None and prev_sh > 0 else 0.0
    except (TypeError, ValueError):
        prev_sh, usd = 0.0, 0.0
    total = prev_sh if usd > 0 else 0.0
    added = False
    for item in fills or ():
        try:
            shares, px = float(item[0]), item[1]
            if px is None:
                continue
            px = float(px)
        except (TypeError, ValueError, IndexError):
            continue
        if not (math.isfinite(shares) and math.isfinite(px)) or shares <= 1e-12 or px <= 0:
            continue
        usd += shares * px
        total += shares
        added = True
    if not added:
        return intent.get(key)
    avg = round(usd / total, 4)
    intent[key] = avg
    intent[key + "_shares"] = round(total, 6)
    return avg


def kept_loser_open(intent: Any) -> bool:
    """True while a partial scrap still holds kept loser shares.

    Kept shares leave through the winner path when the kept leg wins
    (``sell_winner_leg == sold_leg``), at resolution, or with the held
    dump when ``sell_dump_also_kept`` is on (``sell_dump_kept_sold``).
    """
    if not isinstance(intent, dict):
        return False
    try:
        keep = float(intent.get("sell_scrap_keep") or 0.0)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(keep) or keep <= 1e-12:
        return False
    if intent.get("sell_dump_kept_sold"):
        return False
    sold_leg = intent.get("sold_leg")
    if intent.get("sold_winner") and sold_leg and intent.get("sell_winner_leg") == sold_leg:
        return False
    return True


def recorded_fill_px(intent: dict, fill_key: str, limit_key: str) -> Optional[float]:
    """Average fill if one was priced, else the posted limit (older state)."""
    for key in (fill_key, limit_key):
        raw = intent.get(key)
        if raw is None:
            continue
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(px):
            return px
    return None


def cfg_seconds(cfg: dict, key: str, default: float) -> float:
    """Seconds knob where an explicit 0 means 0.

    Missing, ``null``, empty, non-numeric, non-finite, or negative values
    fall back to ``default``. ``float(cfg.get(key) or default)`` turned a
    0 into the default.
    """
    raw = cfg.get(key) if isinstance(cfg, dict) else None
    if raw is None or raw == "" or isinstance(raw, bool):
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value < 0:
        return float(default)
    return value


def _cents(px: Any) -> str:
    return f"{float(px) * 100:g}c"


def sell_plan_banner(cfg: dict) -> str:
    """One-line description of the loaded sell plan for the startup panel."""
    if not cfg.get("sell_enabled"):
        return "sell off (sell_enabled=false) · keep both legs"
    thr = float(cfg.get("sell_threshold") or DEFAULT_SELL_KNOBS["sell_threshold"])
    floor = float(cfg.get("sell_floor") or DEFAULT_SELL_KNOBS["sell_floor"])
    fak_px = float(cfg.get("sell_fak_px") or thr)
    if bool(cfg.get("sell_scrap_sweep_enabled", True)):
        scrap = f"loser <={_cents(thr)} -> one FAK @ floor {_cents(floor)}"
    else:
        rungs = loser_ladder_limits(thr, floor, thr, fak_px=fak_px)
        scrap = f"loser <={_cents(thr)} -> ladder " + "->".join(_cents(p) for p in rungs)
    parts = [scrap]
    frac = float(cfg.get("sell_scrap_fraction", 1.0) or 1.0)
    if frac < 1.0 - 1e-12:
        parts.append(f"scrap {frac * 100:g}% keep rest")
    scrap_ttm = float(cfg.get("sell_scrap_max_ttm_s") or 0.0)
    if scrap_ttm > 0:
        parts.append(f"scrap ttm<={scrap_ttm:g}s")
    if bool(cfg.get("sell_dump_enabled", True)):
        dump = f"dump held <{_cents(cfg.get('sell_dump_below') or DEFAULT_SELL_KNOBS['sell_dump_below'])}"
        dump_ttm = float(cfg.get("sell_dump_max_ttm_s") or 0.0)
        if dump_ttm > 0:
            dump += f" ttm<={dump_ttm:g}s"
        parts.append(dump)
    winner = cfg.get("sell_winner_min") or DEFAULT_SELL_KNOBS["sell_winner_min"]
    parts.append(f"keep winner (cash >={float(winner):g})")
    return " · ".join(parts)


def loser_scrap_persist_s(
    *,
    now_s: float,
    end_ts: float,
    persist_s: float,
    last_min_s: float,
    last_min_window_s: float,
    depth_at_limit: Optional[float] = None,
    our_size: float = 0.0,
    skip_when_sized: bool = False,
) -> Tuple[Optional[float], str]:
    """Loser persist seconds for this tick, plus a reason.

    ``None`` / ``ended`` when the window is over. ``sized_skip`` (0s) only
    when ``skip_when_sized`` is true and depth at the limit covers
    ``our_size``. Otherwise the normal / last-minute clock from
    ``effective_loser_persist_s``. The last-minute clock applies through
    market close. Does not consult the oracle.
    """
    base = effective_loser_persist_s(
        now_s=now_s,
        end_ts=end_ts,
        persist_s=persist_s,
        last_min_s=last_min_s,
        last_min_window_s=last_min_window_s,
    )
    if base is None:
        return None, "ended"
    if skip_when_sized and depth_covers_size(depth_at_limit, our_size):
        return 0.0, "sized_skip"
    if end_ts:
        ttm = float(end_ts) - float(now_s)
        window = float(last_min_window_s or 0)
        if window > 0 and 0 < ttm <= window + 1e-12:
            return float(base), "last_min"
    return float(base), "normal"


def loser_blind_fak_due(
    *,
    why: str,
    now_s: float,
    last_blind_at: Optional[float],
    backoff_s: float,
    sold_loser: bool,
    has_inventory: bool,
    rest_live: bool,
    oracle_blocks: bool,
    enabled: bool = True,
) -> Tuple[bool, str]:
    """Whether to post a blind floor-tick FAK while the loser arm is kept."""
    if not enabled:
        return False, "disabled"
    if str(why or "") not in ("empty_keep_arm", "empty_fak_keep_arm"):
        return False, "not_empty_arm"
    if sold_loser:
        return False, "sold"
    if rest_live:
        return False, "rest_live"
    if oracle_blocks:
        return False, "oracle_block"
    if not has_inventory:
        return False, "no_inventory"
    if last_blind_at is not None:
        wait = float(backoff_s or 0)
        if float(now_s) + 1e-12 < float(last_blind_at) + wait:
            return False, "backoff"
    return True, "blind_fak"


def scrap_rest_action(
    *,
    enabled: bool,
    rest_order_id: Optional[str],
    sold_loser: bool,
    window_open: bool,
    loser_qualifies: bool,
    oracle_blocks: bool,
    fak_miss: bool,
    armed: bool,
) -> Tuple[str, str]:
    """What to do with the post-miss resting scrap sell.

    Returns ``place``, ``keep``, ``cancel``, or ``none``, plus a reason.
    An open rest is cancelled when the window ends, the loser is done, the
    loser no longer qualifies, or the late-window oracle hard-blocks.
    A new rest is placed only after a FAK miss while still armed, and only
    when the oracle is not blocking.
    """
    if rest_order_id:
        if not window_open:
            return "cancel", "window_end"
        if sold_loser:
            return "cancel", "filled"
        if oracle_blocks:
            return "cancel", "oracle_block"
        if not loser_qualifies:
            return "cancel", "loser_disqualified"
        return "keep", "resting"
    if not enabled:
        return "none", "disabled"
    if not window_open:
        return "none", "window_end"
    if sold_loser or not armed:
        return "none", "not_armed"
    if oracle_blocks:
        return "none", "oracle_block"
    if not loser_qualifies:
        return "none", "not_qualify"
    if not fak_miss:
        return "none", "no_fak_miss"
    return "place", "fak_miss"


def scrap_rest_px(configured_px: float, best_bid: Optional[float]) -> float:
    """Post-miss rest price: never above the live loser bid.

    ``configured_px`` is ``sell_scrap_rest_px`` (the ~2¢ print, kept as the
    ceiling). A positive ``best_bid`` caps the rest at that bid so a 1¢
    book can still be hit. A missing or non-positive bid keeps the
    configured price.
    """
    try:
        cfg = float(configured_px)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(cfg):
        return 0.0
    if best_bid is None:
        return cfg
    try:
        bid = float(best_bid)
    except (TypeError, ValueError):
        return cfg
    if not math.isfinite(bid) or bid <= 0:
        return cfg
    return min(cfg, bid)


def resting_tif(
    *, now_s: float, expire_ts: float, min_ahead_s: float = 180.0
) -> Tuple[str, int]:
    """``(GTD, unix_exp)`` when expiration is far enough ahead, else ``(GTC, 0)``.

    Polymarket rejects a GTD that expires inside the ~180s security threshold.
    Callers still cancel GTC rests at window end or T−cancel.
    """
    try:
        exp = int(float(expire_ts))
        now_i = int(float(now_s))
        ahead = int(float(min_ahead_s or 0))
    except (TypeError, ValueError):
        return "GTC", 0
    if exp >= now_i + max(ahead, 0) + 1:
        return "GTD", exp
    return "GTC", 0


def posted_order_id(result: Any) -> Optional[str]:
    """CLOB post response order id, if one was returned."""
    if isinstance(result, str) and result.strip():
        return result.strip()
    if not isinstance(result, dict):
        return None
    for key in ("orderID", "orderId", "id"):
        value = result.get(key)
        if value:
            return str(value)
    return None


def rest_order_matched_shares(order: Any, offered: float) -> Tuple[float, str]:
    """``(matched_shares, live|filled|cancelled|unknown)`` from ``get_order``."""
    if not isinstance(order, dict):
        return 0.0, "unknown"
    status = str(
        order.get("status") or order.get("order_status") or ""
    ).lower()
    probe = {
        "size_matched": order.get("size_matched", order.get("sizeMatched")),
        "makingAmount": order.get("makingAmount", order.get("making_amount")),
    }
    matched = parse_sell_fill_shares(probe, offered)
    if "cancel" in status:
        return matched, "cancelled"
    offered_f = float(offered or 0)
    if offered_f > 0 and matched + 1e-9 >= offered_f - 1e-9:
        return matched, "filled"
    if status in {"matched", "filled", "order_status_matched"}:
        if matched <= 0 and offered_f > 0:
            return offered_f, "filled"
        return matched, "filled"
    return matched, "live"
