"""Hedge persist gate and CLOB tick helpers (no I/O). Toxic dumps stay instant."""

from __future__ import annotations

import re
from typing import NamedTuple, Optional, Tuple

_CLOB_TICKS = (0.1, 0.01, 0.005, 0.0025, 0.001, 0.0001)
_MIN_TICK_RE = re.compile(r"minimum is\s+(0\.\d+)", re.IGNORECASE)


def hedge_persist_ready(
    qualifies: bool,
    *,
    now_s: float,
    armed_ts: Optional[float],
    persist_s: float,
    toxic: bool = False,
) -> Tuple[bool, Optional[float], str]:
    """Whether a qualifying hedge book may sell yet.

    Returns ``(fire, new_armed_ts, why)``.

    ``persist_s`` ≤ 0 or ``toxic`` sells on the first qualifying tick.
    A failed book clears the arm so a one-tick dip does not count.
    """
    if not qualifies:
        return False, None, "reset"
    try:
        wait = float(persist_s or 0)
    except (TypeError, ValueError):
        wait = 0.0
    if toxic or wait <= 1e-12:
        return True, armed_ts, "immediate"
    if armed_ts is None:
        return False, float(now_s), "armed"
    if float(now_s) - float(armed_ts) < wait - 1e-12:
        return False, float(armed_ts), "waiting"
    return True, float(armed_ts), "ready"


def _known_clob_tick(raw: float) -> Optional[float]:
    for known in _CLOB_TICKS:
        if abs(float(raw) - known) < 1e-12:
            return known
    return None


def hedge_market_tick(reported, expected=0.001) -> float:
    """Tick the CLOB will accept for a 5m hedge FAK.

    Honor a *coarser* market tick. Forcing 0.001 on a 0.01 book rejects the
    signed order (``invalid tick size (0.001), minimum is 0.01``) and the
    dump never sells (live 22 Aug 11:40: persist fired at 61/62, then
    ``[EXIT FAIL]``).

    The 21 Aug unmatched 0.51-into-0.53 hole was ``hedge_undercut_ticks=2``
    on a 0.01 tick, not "must post 0.001". Live undercut stays 0: sell at
    the live bid aligned to this tick.
    """
    try:
        exp = float(expected) if expected not in (None, "") else 0.001
    except (TypeError, ValueError):
        exp = 0.001
    if exp <= 0:
        exp = 0.001
    try:
        raw = float(reported) if reported not in (None, "") else exp
    except (TypeError, ValueError):
        raw = exp
    if raw <= 0:
        raw = exp
    known = _known_clob_tick(raw)
    if known is not None:
        raw = known
    known_exp = _known_clob_tick(exp)
    if known_exp is not None:
        exp = known_exp
    return max(raw, exp)


def clob_min_tick_from_error(error) -> Optional[float]:
    """Parse ``invalid tick size (0.001), minimum is 0.01`` from a CLOB error."""
    match = _MIN_TICK_RE.search(str(error or ""))
    if not match:
        return None
    try:
        tick = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if tick <= 0:
        return None
    return _known_clob_tick(tick) or tick


def hedge_tick_after_build_error(current_tick, error) -> Optional[float]:
    """If CLOB rejected a too-fine tick, return the minimum we must retry with."""
    minimum = clob_min_tick_from_error(error)
    if minimum is None:
        return None
    try:
        current = float(current_tick)
    except (TypeError, ValueError):
        current = 0.0
    if minimum > current + 1e-12:
        return minimum
    return None


class HedgeIntent(NamedTuple):
    """What a live 5m bag should do this tick (no I/O)."""

    action: str
    reason: str
    sell_at: Optional[float]
    persist_ts: Optional[float]
    persist_done: bool
    skip_gui: bool
    abort_above: Optional[float]
    dump: bool


def _finite_px(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        px = float(value)
    except (TypeError, ValueError):
        return None
    if px < 0 or px > 1:
        return None
    return px


def pick_held_quote(rest_bid, rest_ask, ws_bid, ws_ask, last_bid, last_ask):
    """REST, then WS, then last-good. Bid-only is enough to dump.

    Incomplete REST must not skip a live bag (22 Aug: 2176
    ``hedge_skip_incomplete_rest`` while 09:35 / 11:25 rode to zero).
    """
    for bid, ask in (
        (rest_bid, rest_ask),
        (ws_bid, ws_ask),
        (last_bid, last_ask),
    ):
        b = _finite_px(bid)
        if b is None:
            continue
        return b, _finite_px(ask)
    return None, None


def hedge_qualify_ok(bid, ask, threshold, max_spread, require_ask_max):
    """Tight 70/72 book. Missing ask cannot qualify persist (dump is bid-only)."""
    bid_f = _finite_px(bid)
    ask_f = _finite_px(ask)
    try:
        thr = float(threshold)
        spread_max = float(max_spread)
        ask_max = float(require_ask_max)
    except (TypeError, ValueError):
        return False, "missing_side"
    if bid_f is None or ask_f is None:
        return False, "missing_side"
    if bid_f > thr + 1e-12:
        return False, "bid_above"
    if ask_f > ask_max + 1e-12:
        return False, "ask_too_high"
    if ask_f < bid_f:
        return False, "crossed"
    if (ask_f - bid_f) > spread_max + 1e-12:
        return False, "wide_spread"
    return True, "ok"


def evaluate_held_bag(
    bid,
    ask=None,
    *,
    now_s,
    persist_armed_ts,
    persist_s=2.0,
    dump_bid_max=0.53,
    qualify_bid=0.70,
    qualify_ask_max=0.72,
    max_spread=0.15,
    persist_done=False,
    gui_ok=True,
    gui_why="ok",
    recovery_cancel=0.85,
    sell_fade=False,
):
    """Dump / persist-sell / hold for one live bag.

    * Bid ≤ dump dumps every bag. Bid-only. No GUI / last-trade veto.
      Wide 22/77 still dumps.
    * 5m default: do not sell in (dump, qualify). Persist-not-done 61/70
      is a hold. After persist, qualify–recovery live-bid sells (70–84).
      Bid ≥ recovery_cancel (default 85¢) is a recovered winner: HOLD and
      clear persist. Do not sell 90–99¢ because persist_done stuck.
    * Hourly (``sell_fade``): after persist, sell any bid still below
      recovery — including a fade through qualify — instead of waiting
      for the dump print. Tight recovery (53¢) is what stops 50–69 fills.
    * Persist qualify is still the tight book (GUI applies only there).
    """
    bid_f = _finite_px(bid)
    ask_f = _finite_px(ask)
    try:
        dump_max = float(dump_bid_max)
        qualify = float(qualify_bid)
        wait = float(persist_s or 0)
        recovery = float(recovery_cancel)
    except (TypeError, ValueError):
        return HedgeIntent("hold", "bad_thresholds", None, None, False, True, None, False)
    if not (dump_max < qualify <= recovery <= 1):
        return HedgeIntent("hold", "bad_thresholds", None, None, False, True, None, False)

    done = bool(persist_done)
    if persist_armed_ts is not None and wait > 1e-12:
        try:
            if float(now_s) - float(persist_armed_ts) >= wait - 1e-12:
                done = True
        except (TypeError, ValueError):
            pass

    if bid_f is None:
        return HedgeIntent(
            "hold", "no_bid", None, persist_armed_ts, done, True, None, False,
        )

    if bid_f <= dump_max + 1e-12:
        return HedgeIntent(
            "dump", "bid_le_dump", bid_f, persist_armed_ts, done, True, dump_max, True,
        )

    if bid_f + 1e-12 >= recovery:
        return HedgeIntent(
            "hold", "recovery_cancel", None, None, False, True, None, False,
        )

    if done:
        if bid_f + 1e-12 >= qualify:
            return HedgeIntent(
                "sell", "persist_live_bid", bid_f, persist_armed_ts, True, True,
                recovery, False,
            )
        if sell_fade:
            return HedgeIntent(
                "sell", "persist_live_bid", bid_f, persist_armed_ts, True, True,
                recovery, False,
            )
        return HedgeIntent(
            "hold", "dead_band", None, persist_armed_ts, True, True, None, False,
        )

    ok, why = hedge_qualify_ok(
        bid_f, ask_f, qualify, max_spread, qualify_ask_max,
    )
    if not ok:
        return HedgeIntent("hold", why, None, None, False, False, None, False)

    if not gui_ok:
        return HedgeIntent(
            "hold", str(gui_why or "no_consensus"), None, None, False, False, None, False,
        )

    fire, new_ts, pwhy = hedge_persist_ready(
        True, now_s=float(now_s), armed_ts=persist_armed_ts, persist_s=wait,
    )
    if fire:
        if dump_max < bid_f < qualify - 1e-12:
            return HedgeIntent(
                "hold", "dead_band", None, new_ts, True, False, None, False,
            )
        return HedgeIntent(
            "sell", "persist_ready", bid_f, new_ts, True, False, recovery, False,
        )
    if pwhy == "armed":
        return HedgeIntent("arm", "persist_armed", None, new_ts, False, False, None, False)
    return HedgeIntent("wait", "persist_waiting", None, new_ts, False, False, None, False)


def hedge_should_keep_retrying(
    remaining,
    bid,
    *,
    persist_done=False,
    dump_bid_max=0.53,
    qualify_bid=0.70,
    recovery_cancel=0.85,
    sell_fade=False,
) -> bool:
    """Unmatched / invalid-tick / could-not-run is not terminal while size remains."""
    try:
        rem = float(remaining or 0)
    except (TypeError, ValueError):
        rem = 0.0
    if rem < 0.01:
        return False
    bid_f = _finite_px(bid)
    if bid_f is None:
        return True
    try:
        dump_max = float(dump_bid_max)
        qualify = float(qualify_bid)
        recovery = float(recovery_cancel)
    except (TypeError, ValueError):
        return True
    if bid_f <= dump_max + 1e-12:
        return True
    if persist_done and bid_f < recovery - 1e-12:
        if bid_f + 1e-12 >= qualify or sell_fade:
            return True
    return False


def hedge_fail_is_terminal(
    sell_status,
    remaining,
    bid,
    *,
    persist_done=False,
    dump_bid_max=0.53,
    qualify_bid=0.70,
    recovery_cancel=0.85,
    sell_fade=False,
) -> bool:
    """``hedge_fail`` after ``sell_attempt_rejected`` must not idle a live dump."""
    status = str(sell_status or "")
    if status == "ambiguous":
        return True
    return not hedge_should_keep_retrying(
        remaining,
        bid,
        persist_done=persist_done,
        dump_bid_max=dump_bid_max,
        qualify_bid=qualify_bid,
        recovery_cancel=recovery_cancel,
        sell_fade=sell_fade,
    )


def should_mark_hedge_closed(sold, remaining) -> bool:
    """Full hedge_closed only after confirmed inventory is gone."""
    try:
        sold_f = float(sold or 0)
        rem = float(remaining or 0)
    except (TypeError, ValueError):
        return False
    return sold_f > 0.01 and rem < 0.01


def live_bag_log_fields(
    *,
    slug=None,
    ttm=None,
    bid=None,
    ask=None,
    tick=None,
    reason=None,
    order_error=None,
):
    """Every live-bag skip/fail must carry slug/ttm/bid/ask/tick/reason."""
    payload = {
        "slug": slug,
        "ttm": None if ttm is None else round(float(ttm), 1),
        "bid": bid,
        "ask": ask,
        "tick": tick,
    }
    if reason is not None:
        payload["reason"] = reason
    if order_error is not None:
        payload["order_error"] = str(order_error)[:200]
    return payload


def hedge_oracle_allows_sell(held_leg, check, *, enabled=True):
    """Once holding, do not sell while live BTC is still on the held side of PTB.

    CLOB one-ticks and unreflective TOB are not a hedge if the resolution
    oracle still says the held leg wins. Missing/stale oracle also blocks
    the sell (fail closed against false hedges). A flipped or exactly-flat
    oracle lets the book persist/dump path continue.
    """
    if not enabled:
        return True, "oracle_off"
    leg = str(held_leg or "").strip().lower()
    if leg not in ("up", "down"):
        return False, "oracle_bad_leg"
    if not isinstance(check, dict):
        return False, "oracle_unknown"
    favored = check.get("favored")
    if favored == leg:
        return False, "oracle_still_winning"
    if favored in ("up", "down") and favored != leg:
        return True, "oracle_against"
    if str(check.get("reason") or "") == "edge_zero":
        return True, "oracle_flat"
    return False, "oracle_unknown"
