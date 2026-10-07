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
live-bid FAK the held leg. Optional ``sell_dump_persist_last_min_s`` applies
when time-to-end is within ``sell_dump_persist_last_min_window_s`` (default 0,
off). ``sell_dump_max_ttm_s`` (code default 0, off)
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
    # Dump persist inside the last window seconds. None = same as sell_dump_persist_s.
    # Window 0 disables the last-minute clock.
    "sell_dump_persist_last_min_s": None,
    "sell_dump_persist_last_min_window_s": 0.0,
    # After first dump no-match/kill-0-fill, quickly refire this many times.
    "sell_dump_fak_retries": 2,
    # Dump retry ladder: top bid, then step down toward floor (short burst).
    "sell_dump_ladder_step": 0.04,
    "sell_dump_ladder_rungs": 4,
    # 0 disables the time-left gate (old behavior). The example sets 240.
    "sell_dump_max_ttm_s": 0.0,
    # When the held dump fills, also sell the kept scrap half (1c floor sweep).
    "sell_dump_also_kept": False,
    # After a both-sides dump, optional FAK buy of the side that reclaims.
    # Off until the operator sets reclaim_enabled. The stop is on unless
    # reclaim_stop_enabled is set false (hold to redeem).
    "reclaim_enabled": False,
    "reclaim_usd": 100.0,
    "reclaim_entry": 0.91,
    # How long the entry print must hold. Same default as the dump persist
    # (0.5s). Explicit 0 fires on the tick the book first qualifies
    # (persist_ready). Negatives are rejected.
    "reclaim_entry_persist_s": 0.5,
    "reclaim_stop": 0.75,
    "reclaim_stop_enabled": True,
    # Stop clock. Same default as the entry and the dump persist.
    "reclaim_stop_persist_s": 0.5,
    # Marketable FAK may pay this much over the observed ask, hard-capped.
    "reclaim_slippage": 0.03,
    "reclaim_max_price": 0.96,
    # 0 leaves the entry time gate off (scrap_time_gate_open). Unknown ttm stays open.
    "reclaim_max_ttm_s": 0.0,
    # 0 leaves the close gate off. Above 0, no reclaim entry when ttm is under it.
    "reclaim_min_ttm_s": 0.0,
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
    # Any-time scrap veto: no loser scrap while the 60s TWAP or (use_live)
    # the live Chainlink price is within $usd of the strike or on the
    # scrapped leg's side. A stale reading drops out; both stale: no veto.
    "scrap_oracle_veto_enabled": False,
    "scrap_oracle_veto_usd": 5.0,
    "scrap_oracle_veto_stale_s": 3.0,
    "scrap_oracle_veto_use_live": True,
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
    - a both-sides dump is done and ``reclaim_hot`` is set (entry watch
      or a live stop). Cleared when the reclaim finishes.

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
        if not sold_exit:
            return True
        # Set by the sell loop only while reclaim_enabled and the bag can
        # still buy or stop. Keeps sell_armed_poll_s until that finishes.
        return bool(intent.get("reclaim_hot"))
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


def effective_dump_persist_s(
    *,
    now_s: float,
    end_ts: float,
    persist_s: float,
    last_min_s: float,
    last_min_window_s: float,
) -> float:
    """Held-leg dump persist for this tick.

    Same clock switch as ``effective_loser_persist_s``. Falls back to
    ``persist_s`` once the market has ended. Callers pass the result into
    ``persist_ready`` each tick, so ``armed_ts`` is never reset on a switch.
    """
    effective = effective_loser_persist_s(
        now_s=now_s,
        end_ts=end_ts,
        persist_s=persist_s,
        last_min_s=last_min_s,
        last_min_window_s=last_min_window_s,
    )
    return float(persist_s) if effective is None else effective


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


def scrap_oracle_settings(cfg: Any) -> Tuple[bool, float, float, bool]:
    """``(enabled, threshold_usd, stale_s, use_live)`` for the scrap oracle veto.

    A missing, non-numeric, non-finite or negative value takes the default
    (off, $5, 3s, live on).
    """
    get = cfg.get if isinstance(cfg, dict) else (lambda _k, d=None: d)

    def _flag(key: str) -> bool:
        raw = get(key, False if key == "scrap_oracle_veto_enabled" else True)
        if isinstance(raw, str):
            return raw.strip().lower() not in ("0", "false", "no", "off", "")
        return bool(raw)

    def _num(key: str, default: float) -> float:
        val = _finite_usd(get(key, default))
        if val is None or val < 0:
            return default
        return val

    return (
        _flag("scrap_oracle_veto_enabled"),
        _num("scrap_oracle_veto_usd", 5.0),
        _num("scrap_oracle_veto_stale_s", 3.0),
        _flag("scrap_oracle_veto_use_live"),
    )


# Chainlink obs stamps reach us ~1.5-2.5s late; past stale_s + this the
# reading is old even if the frame itself just arrived.
SCRAP_ORACLE_OBS_LAG_S = 7.0


def _scrap_source_check(
    value: Optional[float],
    age_s: Optional[float],
    obs_age_s: Optional[float],
    stale_s: float,
    name: str,
) -> Optional[str]:
    """None when the reading is usable, else why not (``missing_twap`` ...)."""
    if value is None:
        return f"missing_{name}"
    if age_s is None:
        return f"missing_{name}_age"
    if float(age_s) > float(stale_s) + 1e-12:
        return f"stale_{name}"
    if obs_age_s is not None and float(obs_age_s) > float(stale_s) + SCRAP_ORACLE_OBS_LAG_S:
        return "stale_obs" if name == "twap" else f"stale_{name}_obs"
    return None


def _scrap_side_verdict(leg: str, margin: float, thr: float) -> Optional[str]:
    """``favors`` / ``within`` when this margin vetoes scrapping ``leg``."""
    if leg == "up":
        if margin > -thr:
            return "favors" if margin > 0 else "within"
    elif margin < thr:
        return "favors" if margin < 0 else "within"
    return None


def _round_age(age: Optional[float]) -> Optional[float]:
    return None if age is None else round(float(age), 3)


def scrap_oracle_veto(
    *,
    scrap_leg: Optional[str],
    twap_usd: Any,
    strike_usd: Any,
    twap_age_s: Optional[float],
    threshold_usd: float,
    stale_s: float,
    enabled: bool = True,
    obs_age_s: Optional[float] = None,
    live_usd: Any = None,
    live_age_s: Optional[float] = None,
    live_obs_age_s: Optional[float] = None,
    use_live: bool = False,
) -> Tuple[bool, str, dict]:
    """``(block, why, detail)``: veto a loser scrap the oracle still favours.

    ``margin = twap - strike`` and, with ``use_live``, ``live_margin =
    live - strike``. Scrapping Up is blocked while either margin is
    ``> -threshold``; scrapping Down while either is ``< +threshold``. So
    the scrap only goes ahead when every fresh reading is more than
    ``threshold`` against the leg. Ages are local receive ages;
    ``*obs_age_s`` (optional) are the Chainlink stamp ages.

    A stale or missing reading drops out (``detail["fallback"]`` is
    ``twap_only`` / ``live_only``). With neither usable, or no strike,
    ``block=False`` with the reason, so the scrap runs as before the veto.
    """
    thr = float(threshold_usd)
    detail: dict = {
        "leg": scrap_leg,
        "twap": None,
        "strike": None,
        "margin": None,
        "live_price": None,
        "live_margin": None,
        "threshold": thr,
        "age_s": _round_age(twap_age_s),
        "obs_age_s": _round_age(obs_age_s),
        "live_age_s": _round_age(live_age_s),
        "live_obs_age_s": _round_age(live_obs_age_s),
        "twap_why": None,
        "live_why": None if use_live else "live_off",
        "basis": None,
        "fallback": None,
    }
    if not enabled:
        return False, "disabled", detail
    if scrap_leg not in ("up", "dn"):
        return False, "bad_leg", detail
    strike = _finite_usd(strike_usd)
    twap = _finite_usd(twap_usd)
    live = _finite_usd(live_usd) if use_live else None
    detail["strike"] = strike
    detail["twap"] = twap
    detail["live_price"] = live
    if strike is None:
        detail["fallback"] = "none"
        return False, "missing_strike", detail
    twap_bad = _scrap_source_check(twap, twap_age_s, obs_age_s, stale_s, "twap")
    twap_verdict = None
    if twap_bad is None:
        detail["margin"] = round(twap - strike, 4)
        twap_verdict = _scrap_side_verdict(scrap_leg, detail["margin"], thr)
    detail["twap_why"] = twap_bad or (twap_verdict or "clear_against")
    live_bad: Optional[str] = "live_off"
    live_verdict = None
    if use_live:
        live_bad = _scrap_source_check(live, live_age_s, live_obs_age_s, stale_s, "live")
        if live_bad is None:
            detail["live_margin"] = round(live - strike, 4)
            live_verdict = _scrap_side_verdict(scrap_leg, detail["live_margin"], thr)
        detail["live_why"] = live_bad or (live_verdict or "clear_against")
    if twap_bad is not None and live_bad is not None:
        detail["fallback"] = "none"
        return False, twap_bad, detail
    if twap_bad is None and live_bad is None:
        detail["basis"] = "twap+live"
    elif twap_bad is None:
        detail["basis"] = "twap"
        if use_live:
            detail["fallback"] = "twap_only"
    else:
        detail["basis"] = "live"
        detail["fallback"] = "live_only"
    if twap_verdict is not None:
        return True, ("oracle_favors_leg" if twap_verdict == "favors" else "within_threshold"), detail
    if live_verdict is not None:
        return True, ("live_favors_leg" if live_verdict == "favors" else "live_within_threshold"), detail
    return False, "clear_against", detail


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


def scrap_live_bid_limit(
    loser_bid: Optional[float],
    threshold: float,
    *,
    min_px: float = 0.01,
) -> float:
    """Sweep FAK limit once the loser scrap is triggered: the live bid.

    The trigger (loser bid <= ``sell_threshold`` with the favourite >=
    ``sell_opposite_min``) is the ceiling, not the print. Once triggered the
    FAK posts at the current best bid so it chases the bid down tick by
    tick. It is capped at ``threshold`` and floored only at the exchange
    minimum ``min_px`` (``sell_clob_min_price``, 1¢). ``sell_floor`` is NOT
    applied: live bag ``btc-updown-15m-1791215100`` had ``sell_floor`` 0.09,
    so every retry posted ``limit>=0.090`` into an 8¢/7¢/1¢ book, missed
    ~45 times and left 39 Up to resolve at $0.
    """
    try:
        lo = float(min_px)
    except (TypeError, ValueError):
        lo = 0.01
    if not math.isfinite(lo) or lo <= 0:
        lo = 0.01
    lo = round(lo, 4)
    try:
        bid = float(loser_bid) if loser_bid is not None else 0.0
    except (TypeError, ValueError):
        bid = 0.0
    if not math.isfinite(bid) or bid <= 1e-12:
        return lo
    try:
        top = float(threshold)
    except (TypeError, ValueError):
        top = bid
    if math.isfinite(top) and top > 0:
        bid = min(bid, top)
    return round(max(lo, bid), 4)


def loser_scrap_post(
    *,
    sweep: bool,
    remaining: float,
    floor: float,
    threshold: float,
    loser_bid: float,
    fak_px: Optional[float] = None,
    depth_at_limit: Optional[float] = None,
    min_px: float = 0.01,
) -> dict:
    """Loser scrap order for this fire.

    Sweep posts one FAK at the live loser bid (``scrap_live_bid_limit``:
    capped at ``threshold``, floored at ``min_px``) for the full
    remainder. Any shares left are retried next tick at the new live bid.
    ``floor`` does not clamp the sweep up. Flag off keeps the 1¢ ladder and
    the top-rung depth clip.
    """
    rem = max(0.0, float(remaining or 0.0))
    if sweep:
        return {
            "mode": "sweep",
            "limits": [scrap_live_bid_limit(loser_bid, threshold, min_px=min_px)],
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


def buy_fill_vwap(result: Any, bought_shares: float) -> Optional[float]:
    """USDC per share from a BUY fill. ``makingAmount`` is collateral paid."""
    try:
        bought = float(bought_shares or 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(bought) or bought <= 1e-12 or not isinstance(result, dict):
        return None
    usdc = None
    for key in ("makingAmount", "making_amount"):
        parsed = _decode_amount(result.get(key), 0.0)
        if parsed is not None and parsed > 0:
            usdc = parsed
            break
    if usdc is None:
        return None
    px = usdc / bought
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


def dump_persist_knobs(cfg: dict) -> Tuple[float, float, float]:
    """``(persist_s, last_min_s, last_min_window_s)`` for the held-leg dump.

    A missing or ``null`` ``sell_dump_persist_last_min_s`` follows
    ``sell_dump_persist_s``. The window defaults to 0 (last-minute clock off).
    """
    persist = cfg_seconds(cfg, "sell_dump_persist_s", 2.0)
    last_min = cfg_seconds(cfg, "sell_dump_persist_last_min_s", persist)
    window = cfg_seconds(cfg, "sell_dump_persist_last_min_window_s", 0.0)
    return persist, last_min, window


# A 91¢ token with a bid more than this far under the ask is not a real print.
# Scrap has no spread knob; this is stricter than the scrap, on purpose.
RECLAIM_MAX_SPREAD = 0.10
# Old scrap tape-stale window. Applied only when the book payload has a timestamp.
RECLAIM_BOOK_MAX_AGE_S = 5.0


def reclaim_complement_max(entry: float) -> float:
    """Other-side ask ceiling. 0.91 entry → 0.09."""
    try:
        px = float(entry)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(px):
        return 0.0
    return round(max(0.0, 1.0 - px), 4)


def reclaim_buy_limit(
    ask: float,
    slippage: float = 0.03,
    max_price: float = 0.96,
) -> Optional[float]:
    """Worst reclaim FAK price: ``min(ask + slippage, max_price)``."""
    try:
        ask_f = float(ask)
        slip = float(slippage)
        cap = float(max_price)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(ask_f)
        or not math.isfinite(slip)
        or not math.isfinite(cap)
        or ask_f <= 0
        or slip < 0
        or not (0 < cap < 1)
    ):
        return None
    limit = min(ask_f + slip, cap)
    if not (0 < limit < 1):
        return None
    return float(limit)


def reclaim_share_size(usd: float, price: float) -> float:
    """``floor(usd / price)`` whole shares. 0 when the price cannot size a buy."""
    try:
        usd_f = float(usd)
        px = float(price)
    except (TypeError, ValueError):
        return 0.0
    if (
        not math.isfinite(usd_f)
        or not math.isfinite(px)
        or usd_f <= 0
        or px <= 0
        or px >= 1
    ):
        return 0.0
    return float(math.floor(usd_f / px + 1e-9))


def reclaim_buy_usdc(shares: float, price: float) -> float:
    """Dollars for a marketable reclaim BUY, truncated to whole cents.

    Polymarket accepts at most two decimals of USDC on a marketable buy.
    The market-order builder rounds that dollar amount down again to the
    tick's size precision (2 on every tick in ``ROUNDING_CONFIG``). A
    notional under one cent returns 0.
    """
    try:
        sh = float(shares)
        px = float(price)
    except (TypeError, ValueError):
        return 0.0
    if (
        not math.isfinite(sh)
        or not math.isfinite(px)
        or sh <= 0
        or px <= 0
        or px >= 1
    ):
        return 0.0
    cents = math.floor(sh * px * 100.0 + 1e-9) / 100.0
    if cents < 0.01:
        return 0.0
    return cents


def reclaim_buy_order_key(shares: float, price: float) -> str:
    """Identity of one reclaim BUY. The same key is the same doomed order."""
    usdc = reclaim_buy_usdc(shares, price)
    try:
        sh = float(shares)
        px = float(price)
    except (TypeError, ValueError):
        return f"bad@{usdc:.2f}"
    if not math.isfinite(sh) or not math.isfinite(px):
        return f"bad@{usdc:.2f}"
    return f"{sh:.4f}@{px:.6f}@{usdc:.2f}"


def reclaim_market_buy_amounts(usdc: float, price: float, tick_size: str) -> dict:
    """Maker and taker units from the real market-order builder.

    ``usdc`` is the BUY amount in dollars. ``maker`` is that USDC in
    6-decimal units. ``taker`` is outcome shares in 6-decimal units.
    """
    from py_clob_client_v2.order_builder.builder import OrderBuilder, ROUNDING_CONFIG
    from py_clob_client_v2.order_builder.constants import BUY

    cfg = ROUNDING_CONFIG[str(tick_size)]
    _side, maker, taker = OrderBuilder(signer=None).get_market_order_amounts(
        BUY, float(usdc), float(price), cfg,
    )
    return {
        "maker": int(maker),
        "taker": int(taker),
        "usdc": int(maker) / 1_000_000,
        "shares": int(taker) / 1_000_000,
        "tick_size": str(tick_size),
        "taker_digits": int(cfg.amount),
    }


def reclaim_fak_status(exc: BaseException) -> str:
    """``reject:`` for a definite refusal, ``error:`` when the fill is unknown.

    HTTP 400 and the hard CLOB messages (invalid amounts, not enough
    balance or allowance) did not land. Timeouts, network errors, and
    5xx stay ``error:`` so the caller keeps the uncertain-fill path.
    """
    text = str(exc).replace("\n", " ").strip()
    low = text.lower()
    status = getattr(exc, "status_code", None)
    code = status if isinstance(status, int) else None
    hard_msg = (
        "invalid amount" in low
        or "not enough balance" in low
        or "not enough allowance" in low
        or "insufficient balance" in low
        or "insufficient allowance" in low
    )
    definite = hard_msg or (
        code is not None and 400 <= code < 500 and code not in (408, 429)
    )
    if code is not None and code >= 500:
        definite = False
    kind = "reject" if definite else "error"
    shown = code if code is not None else "net"
    return f"{kind}:{shown}:{text[:120]}"


def reclaim_arm_block(
    intent: Any,
    *,
    also_kept: bool,
    tol: float = 0.01,
) -> Optional[str]:
    """None when a both-sides dump has finished. Otherwise a skip reason.

    ``sold_dump`` is the durable form of the ``sell_dump_done`` log.
    ``sell_dump_also_kept`` also requires ``sell_dump_kept_done``. A kept
    half that was never sold (flag off, keep > 0) does not arm.
    """
    if not isinstance(intent, dict) or not intent.get("sold_dump"):
        return "no_dump"
    try:
        keep = float(intent.get("sell_scrap_keep") or 0.0)
    except (TypeError, ValueError):
        keep = 0.0
    if not math.isfinite(keep) or keep < 0:
        keep = 0.0
    kept_done = bool(intent.get("sell_dump_kept_done"))
    if also_kept and not kept_done:
        return "kept_pending"
    if keep > float(tol) and not kept_done:
        return "kept_open"
    return None


def reclaim_book_problem(
    bid: Optional[float],
    ask: Optional[float],
    *,
    max_spread: float = RECLAIM_MAX_SPREAD,
) -> Optional[str]:
    """``empty_book`` / ``crossed`` / ``locked`` / ``wide_spread``, or None.

    Locked means bid == ask. Crossed means bid > ask. A spread wider than
    ``max_spread`` is not a usable print.
    """
    if bid is None or ask is None:
        return "empty_book"
    try:
        b = float(bid)
        a = float(ask)
    except (TypeError, ValueError):
        return "empty_book"
    if not math.isfinite(b) or not math.isfinite(a) or not (0 < b < 1) or not (0 < a < 1):
        return "empty_book"
    if b > a + 1e-12:
        return "crossed"
    if abs(b - a) <= 1e-12:
        return "locked"
    try:
        cap = float(max_spread)
    except (TypeError, ValueError):
        cap = RECLAIM_MAX_SPREAD
    if not math.isfinite(cap) or cap <= 0:
        cap = RECLAIM_MAX_SPREAD
    if a - b > cap + 1e-12:
        return "wide_spread"
    return None


def _reclaim_stale(age_s: Optional[float], max_age_s: float) -> bool:
    if age_s is None:
        return False
    try:
        age = float(age_s)
        cap = float(max_age_s)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(age) or not math.isfinite(cap):
        return True
    return age > cap + 1e-12


def reclaim_entry_qualify(
    *,
    leg: str,
    bid: Optional[float],
    ask: Optional[float],
    other_bid: Optional[float],
    other_ask: Optional[float],
    entry: float,
    usd: float,
    order_shares: Optional[float] = None,
    book_age_s: Optional[float] = None,
    other_book_age_s: Optional[float] = None,
    max_age_s: float = RECLAIM_BOOK_MAX_AGE_S,
    max_spread: float = RECLAIM_MAX_SPREAD,
) -> Tuple[bool, str, float]:
    """Gates for buying ``leg``. ``(ok, reason, shares)``.

    The other side confirms when its best ask is at or under
    ``1 - entry`` (0.09 when entry is 0.91). This side's ask must clear
    the entry. The posted limit is ``reclaim_buy_limit``, not this ask.
    There is no ask-depth gate. An empty,
    crossed, locked, or wide book resets; it does not keep the arm.
    """
    if leg not in ("up", "dn"):
        return False, "bad_leg", 0.0
    if _reclaim_stale(book_age_s, max_age_s) or _reclaim_stale(other_book_age_s, max_age_s):
        return False, "stale_book", 0.0
    own = reclaim_book_problem(bid, ask, max_spread=max_spread)
    if own:
        return False, own, 0.0
    other = reclaim_book_problem(other_bid, other_ask, max_spread=max_spread)
    if other:
        return False, other, 0.0
    try:
        entry_f = float(entry)
        ask_f = float(ask)
        other_ask_f = float(other_ask)
    except (TypeError, ValueError):
        return False, "empty_book", 0.0
    if not math.isfinite(entry_f) or not (0 < entry_f < 1):
        return False, "bad_entry", 0.0
    if ask_f + 1e-12 >= entry_f and other_ask_f + 1e-12 >= entry_f:
        return False, "both_rich", 0.0
    if other_ask_f > reclaim_complement_max(entry_f) + 1e-12:
        return False, "sister_unconfirmed", 0.0
    if ask_f + 1e-12 < entry_f:
        return False, "below_entry", 0.0
    if order_shares is None:
        shares = reclaim_share_size(usd, ask_f)
    else:
        try:
            shares = float(order_shares)
        except (TypeError, ValueError):
            shares = 0.0
        if not math.isfinite(shares) or shares < 0:
            shares = 0.0
    if shares < 1:
        return False, "size_zero", 0.0
    return True, "ok", shares


def reclaim_candidate_order(
    up_ask: Optional[float],
    dn_ask: Optional[float],
    entry: float,
) -> list:
    """Legs whose ask is at/over ``entry``, higher ask first. Tie → up."""
    rows = []
    try:
        entry_f = float(entry)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(entry_f):
        return []
    for leg, ask in (("up", up_ask), ("dn", dn_ask)):
        try:
            px = float(ask) if ask is not None else None
        except (TypeError, ValueError):
            px = None
        if px is None or not math.isfinite(px) or px + 1e-12 < entry_f:
            continue
        rows.append((px, 0 if leg == "up" else 1, leg))
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [leg for _px, _tie, leg in rows]


def reclaim_too_close(ttm_s: Optional[float], min_ttm_s: Optional[float]) -> bool:
    """True when a positive ``min_ttm_s`` and a known ttm are inside it.

    ``0`` (the default) is off. An unknown ttm stays open, same as the
    max-ttm gate.
    """
    try:
        floor = float(min_ttm_s or 0.0)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(floor) or floor <= 0:
        return False
    if ttm_s is None:
        return False
    try:
        left = float(ttm_s)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(left):
        return False
    return left + 1e-12 < floor


def reclaim_window_open(
    now_s: float,
    end_ts: float,
    ttm_s: Optional[float],
    max_ttm_s: Optional[float],
) -> Tuple[bool, str]:
    """Scrap window gates: ``sell_window_open`` then ``scrap_time_gate_open``."""
    if not sell_window_open(now_s, float(end_ts or 0)):
        return False, "window_closed"
    if not scrap_time_gate_open(ttm_s, max_ttm_s):
        return False, "time_gated"
    return True, "open"


def _reclaim_quote(
    leg: str,
    *,
    up_bid,
    up_ask,
    dn_bid,
    dn_ask,
    up_age,
    dn_age,
    entry: float,
    usd: float,
    order_shares: Optional[float],
    max_age_s: float,
    max_spread: float,
) -> Tuple[bool, str, float]:
    if leg == "up":
        return reclaim_entry_qualify(
            leg="up",
            bid=up_bid,
            ask=up_ask,
            other_bid=dn_bid,
            other_ask=dn_ask,
            entry=entry,
            usd=usd,
            order_shares=order_shares,
            book_age_s=up_age,
            other_book_age_s=dn_age,
            max_age_s=max_age_s,
            max_spread=max_spread,
        )
    return reclaim_entry_qualify(
        leg="dn",
        bid=dn_bid,
        ask=dn_ask,
        other_bid=up_bid,
        other_ask=up_ask,
        entry=entry,
        usd=usd,
        order_shares=order_shares,
        book_age_s=dn_age,
        other_book_age_s=up_age,
        max_age_s=max_age_s,
        max_spread=max_spread,
    )


def reclaim_entry_decision(
    *,
    now_s: float,
    end_ts: float,
    ttm_s: Optional[float],
    max_ttm_s: Optional[float],
    min_ttm_s: float = 0.0,
    entry: float,
    usd: float,
    persist_s: float,
    slippage: float = 0.03,
    max_price: float = 0.96,
    armed_ts: Optional[float],
    armed_leg: Optional[str],
    locked_leg: Optional[str],
    filled: float = 0.0,
    target: Optional[float] = None,
    up_bid: Optional[float] = None,
    up_ask: Optional[float] = None,
    dn_bid: Optional[float] = None,
    dn_ask: Optional[float] = None,
    up_age: Optional[float] = None,
    dn_age: Optional[float] = None,
    inflight: bool = False,
    dumped_legs: Optional[Sequence[str]] = None,
    max_age_s: float = RECLAIM_BOOK_MAX_AGE_S,
    max_spread: float = RECLAIM_MAX_SPREAD,
) -> dict:
    """One tick of the reclaim buy. No I/O.

    ``action`` is ``buy``, ``wait``, or ``skip``. A ``buy`` is ready to
    post on this same tick: the persist window has already elapsed, or a
    remainder is retrying a side that already fired. The posted price is
    ``min(ask + slippage, max_price)``. The first clip is sized off that
    limit. Every send, including a re-send, has to pass the entry checks
    on this tick's quotes, including ``ask <= max_price``. A failed check
    clears the persist clock so the next send needs a fresh hold. Callers
    must not sleep or refetch between this result and the FAK.
    """
    dumped = {leg for leg in (dumped_legs or ()) if leg in ("up", "dn")}

    def _out(
        action: str,
        reason: str,
        *,
        leg: Optional[str] = None,
        shares: float = 0.0,
        limit: Optional[float] = None,
        new_armed: Optional[float] = None,
        new_leg: Optional[str] = None,
    ) -> dict:
        return {
            "action": action,
            "reason": reason,
            "leg": leg,
            "shares": float(shares or 0.0),
            "limit": limit,
            "armed_ts": new_armed,
            "armed_leg": new_leg,
            "dumped_leg": bool(leg in dumped) if leg else False,
        }

    if inflight:
        return _out("skip", "inflight", new_armed=armed_ts, new_leg=armed_leg)
    open_ok, open_why = reclaim_window_open(now_s, end_ts, ttm_s, max_ttm_s)
    if not open_ok:
        return _out("skip", open_why)
    if reclaim_too_close(ttm_s, min_ttm_s):
        return _out("skip", "too_close")

    try:
        filled_f = float(filled or 0.0)
    except (TypeError, ValueError):
        filled_f = 0.0
    if not math.isfinite(filled_f) or filled_f < 0:
        filled_f = 0.0
    remainder: Optional[float] = None
    if locked_leg in ("up", "dn") and target is not None:
        try:
            target_f = float(target)
        except (TypeError, ValueError):
            target_f = 0.0
        if math.isfinite(target_f) and target_f > 0:
            remainder = max(0.0, target_f - filled_f)
            if remainder < 1:
                return _out("skip", "filled", leg=locked_leg)

    def _over_cap(leg: str) -> bool:
        ask = up_ask if leg == "up" else dn_ask
        try:
            ask_f = float(ask)
            cap = float(max_price)
        except (TypeError, ValueError):
            return True
        if not math.isfinite(ask_f) or not math.isfinite(cap):
            return True
        return ask_f > cap + 1e-12

    def _qualify(leg: str) -> Tuple[bool, str, float]:
        ok, reason, shares = _reclaim_quote(
            leg,
            up_bid=up_bid,
            up_ask=up_ask,
            dn_bid=dn_bid,
            dn_ask=dn_ask,
            up_age=up_age,
            dn_age=dn_age,
            entry=entry,
            usd=usd,
            order_shares=remainder if locked_leg == leg else None,
            max_age_s=max_age_s,
            max_spread=max_spread,
        )
        if ok and _over_cap(leg):
            return False, "above_cap", 0.0
        return ok, reason, shares

    if locked_leg in ("up", "dn"):
        ok, reason, shares = _qualify(locked_leg)
        if not ok:
            # Failed check: drop the persist clock. The next send has to
            # hold again. Do not keep the old arm and re-send next tick.
            return _out("skip", reason, leg=locked_leg)
        fire, new_armed, why = persist_ready(
            True, now_s=now_s, armed_ts=armed_ts, persist_s=persist_s,
        )
        if not fire:
            return _out(
                "wait", why, leg=locked_leg, shares=shares,
                new_armed=new_armed, new_leg=locked_leg,
            )
        ask = up_ask if locked_leg == "up" else dn_ask
        limit = reclaim_buy_limit(ask, slippage, max_price)
        if limit is None:
            return _out("skip", "above_cap", leg=locked_leg)
        return _out(
            "buy", "retry", leg=locked_leg, shares=shares, limit=limit,
            new_armed=new_armed, new_leg=locked_leg,
        )

    ranked = reclaim_candidate_order(up_ask, dn_ask, entry)
    results = {leg: _qualify(leg) for leg in ("up", "dn")}
    qualifying = [leg for leg in ranked if results[leg][0]]
    if armed_leg in qualifying:
        chosen = armed_leg
        clock = armed_ts
    elif qualifying:
        chosen = qualifying[0]
        clock = None if armed_leg != chosen else armed_ts
    else:
        book_fail = {
            "empty_book", "crossed", "locked", "wide_spread",
            "stale_book", "both_rich", "both_cheap",
        }
        if ranked:
            reason = results[ranked[0]][1]
            report: Optional[str] = ranked[0]
            for leg in ranked:
                why = results[leg][1]
                reason = why
                report = leg
                if leg in dumped:
                    break
            return _out("skip", reason, leg=report)
        reason = "no_entry"
        report = None
        for leg in ("up", "dn"):
            why = results[leg][1]
            if why in book_fail:
                reason = why
                report = leg
                if leg in dumped:
                    break
        return _out("skip", reason, leg=report)

    fire, new_armed, why = persist_ready(
        True, now_s=now_s, armed_ts=clock, persist_s=persist_s,
    )
    _ok, _why, shares = results[chosen]
    ask = up_ask if chosen == "up" else dn_ask
    if not fire:
        return _out(
            "wait", why, leg=chosen, shares=shares, limit=ask,
            new_armed=new_armed, new_leg=chosen,
        )
    # Same quotes that armed the persist. This is the fire re-check.
    ok, reason, _shares = _qualify(chosen)
    if not ok:
        return _out("skip", reason, leg=chosen)
    limit = reclaim_buy_limit(ask, slippage, max_price)
    sized = reclaim_share_size(usd, limit if limit is not None else 0.0)
    if limit is None or sized < 1:
        return _out("skip", "size_zero", leg=chosen)
    return _out(
        "buy", why, leg=chosen, shares=sized, limit=limit,
        new_armed=new_armed, new_leg=chosen,
    )


def reclaim_stop_decision(
    *,
    now_s: float,
    stop: float,
    persist_s: float,
    armed_ts: Optional[float],
    bid: Optional[float],
    latched: bool,
    stop_enabled: bool,
    book_age_s: Optional[float] = None,
    max_age_s: float = RECLAIM_BOOK_MAX_AGE_S,
) -> dict:
    """Stop clock. ``sell`` means post on this tick, at ``limit`` (the bid).

    ``latched`` is after the persist has already fired: later ticks chase
    the live bid with no new wait. A bid back above the stop before the
    latch resets the clock (``persist_ready``).
    """
    if not stop_enabled:
        return {"action": "hold", "reason": "stop_off", "armed_ts": None, "limit": None}
    if armed_ts is not None:
        try:
            if float(armed_ts) > float(now_s):
                armed_ts = float(now_s)
        except (TypeError, ValueError):
            armed_ts = None
    stale = False
    if book_age_s is not None:
        try:
            stale = float(book_age_s) > float(max_age_s) + 1e-12
        except (TypeError, ValueError):
            stale = False
    # A missing or stale quote is not a bid back above the stop. Keep the
    # clock so a tick gap still fires once a live bid is back and the hold
    # has already elapsed.
    if bid is None or stale:
        held_clock = armed_ts is not None or latched
        return {
            "action": "wait" if held_clock else "skip",
            "reason": "stale_book" if stale else "no_bid",
            "armed_ts": armed_ts,
            "limit": None,
        }
    if latched:
        limit = None
        try:
            if float(bid) > 0:
                limit = float(bid)
        except (TypeError, ValueError):
            limit = None
        return {"action": "sell", "reason": "chase", "armed_ts": armed_ts, "limit": limit}
    qualify = False
    try:
        qualify = bid is not None and float(bid) <= float(stop) + 1e-12
    except (TypeError, ValueError):
        qualify = False
    fire, new_armed, why = persist_ready(
        qualify, now_s=now_s, armed_ts=armed_ts, persist_s=persist_s,
    )
    if fire:
        return {
            "action": "sell",
            "reason": why,
            "armed_ts": new_armed,
            "limit": float(bid) if bid is not None else None,
        }
    return {
        "action": "wait" if qualify else "skip",
        "reason": why,
        "armed_ts": new_armed,
        "limit": None,
    }


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
        d_persist, d_last_min, d_window = dump_persist_knobs(cfg)
        if d_window > 0:
            dump += f" persist {d_persist:g}s ({d_last_min:g}s last {d_window:g}s)"
        parts.append(dump)
    winner = cfg.get("sell_winner_min") or DEFAULT_SELL_KNOBS["sell_winner_min"]
    parts.append(f"keep winner (cash >={float(winner):g})")
    if cfg.get("reclaim_enabled"):
        usd = float(cfg.get("reclaim_usd") or DEFAULT_SELL_KNOBS["reclaim_usd"])
        entry = float(cfg.get("reclaim_entry") or DEFAULT_SELL_KNOBS["reclaim_entry"])
        persist = cfg_seconds(
            cfg, "reclaim_entry_persist_s", DEFAULT_SELL_KNOBS["reclaim_entry_persist_s"],
        )
        slip = cfg.get("reclaim_slippage")
        if slip is None:
            slip = DEFAULT_SELL_KNOBS["reclaim_slippage"]
        cap = cfg.get("reclaim_max_price")
        if cap is None:
            cap = DEFAULT_SELL_KNOBS["reclaim_max_price"]
        text = (
            f"reclaim ${usd:g} ask>={_cents(entry)} persist {persist:g}s"
            f" slip {_cents(slip)} cap {_cents(cap)}"
        )
        min_ttm = cfg_seconds(cfg, "reclaim_min_ttm_s", 0.0)
        if min_ttm > 0:
            text += f" ttm>={min_ttm:g}s"
        if cfg.get("reclaim_stop_enabled", True):
            stop = float(cfg.get("reclaim_stop") or DEFAULT_SELL_KNOBS["reclaim_stop"])
            stop_p = cfg_seconds(
                cfg, "reclaim_stop_persist_s", DEFAULT_SELL_KNOBS["reclaim_stop_persist_s"],
            )
            text += f" stop<={_cents(stop)}/{stop_p:g}s"
        else:
            text += " stop off"
        parts.append(text)
    else:
        parts.append("reclaim off")
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
