"""Hedge persist gate and CLOB tick helpers (no I/O).

Toxic dumps stay instant unless ``dump_persist_s`` > 0 (5m V-reversal hold).
``dump_require_tight`` (hourly and 15m) needs ask + spread ≤ max_spread, unless ask ≤ dump_ignore_spread_ask_max (both sides underwater).
"""

from __future__ import annotations

import math

import re
from decimal import Decimal, ROUND_DOWN
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
    dump_armed_ts: Optional[float] = None


class HedgeLadder(NamedTuple):
    """Dump / qualify / ask-max / recovery for one TTM. ``late`` is TTM ≤ cutoff."""

    dump: float
    qualify: float
    ask_max: float
    recovery: float
    late: bool


def hedge_ladder_ok(dump, qualify, ask_max, recovery) -> bool:
    """``dump < qualify <= recovery <= 1`` and ``qualify <= ask_max <= 1``."""
    try:
        dump_f = float(dump)
        qualify_f = float(qualify)
        ask_f = float(ask_max)
        recovery_f = float(recovery)
    except (TypeError, ValueError):
        return False
    if any(x != x for x in (dump_f, qualify_f, ask_f, recovery_f)):
        return False
    return (
        0 < dump_f < qualify_f <= recovery_f <= 1
        and qualify_f <= ask_f <= 1
    )


def hedge_ladder_for_ttm(
    seconds_left,
    dump,
    qualify,
    ask_max,
    recovery,
    late_ttm=30.0,
    late_dump=0.40,
    late_qualify=0.58,
    late_ask_max=0.60,
    late_recovery=0.62,
) -> HedgeLadder:
    """Raise persist/recovery in the last ``late_ttm`` seconds. Dump stays 40.

    TTM > 30 keeps 40 / 50/52 / 53 so a one-tick 50 at T−90 does not dump
    a 75¢ bag. TTM ≤ 30 (including 0 / already-closed) keeps dump **40**
    and uses persist **58/60** / recovery **62**. A last-30s 50/52 must
    still pass GUI + last-trade + 1s persist. A random 50 bid under a
    high ask does not sell.

    Missing / invalid TTM stays on the early rung. ``late_ttm`` ≤ 0 disables
    the late rung. An illegal late rung (not dump < qualify ≤ recovery)
    falls back to early. Late dump may exceed early qualify if an operator
    sets it — do not require late dump ≤ early threshold.
    """
    try:
        early = HedgeLadder(
            float(dump), float(qualify), float(ask_max), float(recovery), False,
        )
    except (TypeError, ValueError):
        early = HedgeLadder(0.40, 0.50, 0.52, 0.53, False)
    try:
        cutoff = float(late_ttm)
    except (TypeError, ValueError):
        return early
    if cutoff != cutoff or cutoff <= 0:
        return early
    if seconds_left is None or seconds_left == "":
        return early
    try:
        ttm = float(seconds_left)
    except (TypeError, ValueError):
        return early
    if ttm != ttm or ttm > cutoff:
        return early
    if not hedge_ladder_ok(late_dump, late_qualify, late_ask_max, late_recovery):
        return early
    return HedgeLadder(
        float(late_dump),
        float(late_qualify),
        float(late_ask_max),
        float(late_recovery),
        True,
    )


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


def implied_held_bid_from_other(other_ask, other_bid=None):
    """Infer held-side bid when the held TOB bid is missing.

    Complementary YES/NO books: if the *other* ask is 99¢, held fair value
    is ~1¢. Use that as a synthetic bid so dump/qualify can still arm when
    the held book has emptied (5m reverse + ``no_bid`` rode to zero).

    Prefer ``1 - other_ask`` (offer to buy the other side). Fall back to
    ``1 - other_bid`` when only the other bid exists. Returns None when
    neither side yields a usable price.
    """
    for raw in (other_ask, other_bid):
        px = _finite_px(raw)
        if px is None:
            continue
        implied = 1.0 - float(px)
        if implied != implied or implied in (float("inf"), float("-inf")):
            continue
        if implied < 0.0:
            implied = 0.0
        if implied > 1.0:
            implied = 1.0
        # Never treat a full-dollar complement as a real held bid — that
        # would mean other_ask ~0 and we are still winning hard.
        if implied >= 1.0 - 1e-12:
            continue
        return implied
    return None


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
    """Tight persist book (live 5m 50/52). Missing ask cannot qualify persist."""
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


def dump_tight_book_hold_reason(
    bid,
    ask,
    *,
    dump_require_tight=False,
    max_spread=0.15,
    dump_ignore_spread_ask_max=0.60,
):
    """If dump must hold for tightness, return the reason; else None.

    Shared by hourly ``evaluate_held_bag`` and the 15m dump arm. Phantom
    penny bids under a still-high ask (34/99) must not dump. Both sides
    underwater (33/50, ask ≤ ``dump_ignore_spread_ask_max``) skip the
    spread check. 5m probe and 15m pass ``dump_require_tight=True``.
    """
    if not dump_require_tight:
        return None
    ask_f = _finite_px(ask)
    if ask_f is None:
        return "dump_missing_ask"
    try:
        ignore_ask_max = float(dump_ignore_spread_ask_max)
    except (TypeError, ValueError):
        ignore_ask_max = 0.60
    both_underwater = ask_f <= ignore_ask_max + 1e-12
    if both_underwater:
        return None
    bid_f = _finite_px(bid)
    if bid_f is None:
        return None
    try:
        spread_max = float(max_spread)
    except (TypeError, ValueError):
        spread_max = 0.20
    if (ask_f - bid_f) > spread_max + 1e-12:
        return "dump_wide_spread"
    return None


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
    flatten=False,
    flatten_max=None,
    dump_persist_s=0.0,
    dump_armed_ts=None,
    dump_require_tight=False,
    dump_ignore_spread_ask_max=0.60,
):
    """Dump / persist-sell / hold for one live bag.

    * Bid ≤ dump dumps every bag. Default is bid-only (no GUI / last-trade
      veto) so wide 22/77 still dumps. ``dump_persist_s`` ≤ 0 is instant.
      When ``dump_require_tight`` (hourly + 15m + 5m probe), also require ask and
      ``ask-bid ≤ max_spread`` for the whole dump persist window — blocks
      34/99 phantom bids. If ask ≤ ``dump_ignore_spread_ask_max``
      (default 60¢), skip the spread check: both sides underwater
      (textbook 33/50 loser). 5m probe now passes ``dump_require_tight``.
      5m live is **2s**: a one-tick 40¢ V-reversal must stay ≤ dump
      before the sell. Flatten walks stay instant.
    * Flatten (5m walks): when ``flatten`` and bid < ``flatten_max``
      (live 75¢ = ``buy_threshold``), dump immediately at the live bid.
      This runs *before* recovery_cancel so a 70¢ walk does not HOLD at
      53¢. Flatten still sets ``dump=True``; with ``hedge_dump_ignore_oracle``
      false the probe still consults oracle / favor-edge before posting.
      Do not raise ``dump_bid_max`` to 75¢ — that breaks dump < qualify.
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

    # Elapsed wall time alone never completes persistence.  The endpoint tick
    # must still pass the current book and GUI checks below.  A caller-provided
    # ``persist_done`` is different: once a previously qualified tick completed
    # persistence, the post-persist fade/recovery policy intentionally remains
    # bid-only.
    done = bool(persist_done)

    if bid_f is None:
        return HedgeIntent(
            "hold", "no_bid", None, persist_armed_ts if done else None,
            done, True, None, False,
        )

    if bid_f <= dump_max + 1e-12:
        # Optional tight-book gate (hourly + 15m): phantom penny bids under a
        # still-high ask must not arm/fire dump.
        tight_why = dump_tight_book_hold_reason(
            bid_f, ask_f,
            dump_require_tight=dump_require_tight,
            max_spread=max_spread,
            dump_ignore_spread_ask_max=dump_ignore_spread_ask_max,
        )
        if tight_why:
            return HedgeIntent(
                "hold", tight_why, None, persist_armed_ts if done else None,
                done, True, None, False, None,
            )
        try:
            dump_wait = float(dump_persist_s or 0)
        except (TypeError, ValueError):
            dump_wait = 0.0
        fire, new_dump_ts, dwhy = hedge_persist_ready(
            True,
            now_s=float(now_s),
            armed_ts=dump_armed_ts,
            persist_s=dump_wait,
        )
        if fire:
            return HedgeIntent(
                "dump", "bid_le_dump", bid_f, persist_armed_ts, done, True,
                dump_max, True, new_dump_ts,
            )
        return HedgeIntent(
            "arm" if dwhy == "armed" else "wait",
            "dump_armed" if dwhy == "armed" else "dump_waiting",
            None, persist_armed_ts, done, True, dump_max, True, new_dump_ts,
        )

    flatten_on = bool(flatten)
    fmax = None
    if flatten_on:
        fmax = _finite_px(flatten_max)
        flatten_on = fmax is not None
    if flatten_on and bid_f + 1e-12 < fmax:
        return HedgeIntent(
            "dump", "flatten_walk", bid_f, persist_armed_ts, done, True, fmax, True,
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
        if dump_max < bid_f < qualify - 1e-12 and not sell_fade:
            return HedgeIntent(
                "hold", "dead_band", None, new_ts, True, False, None, False,
            )
        return HedgeIntent(
            "sell", "persist_live_bid", bid_f, new_ts, True, False, recovery, False,
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
    flatten=False,
    flatten_max=None,
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
    if flatten:
        fmax = _finite_px(flatten_max)
        if fmax is not None and bid_f + 1e-12 < fmax:
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
    flatten=False,
    flatten_max=None,
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
        flatten=flatten,
        flatten_max=flatten_max,
    )


def should_mark_hedge_closed(sold, remaining) -> bool:
    """Full hedge_closed only after confirmed inventory is gone."""
    try:
        sold_f = float(sold or 0)
        rem = float(remaining or 0)
    except (TypeError, ValueError):
        return False
    return sold_f > 0.01 and rem < 0.01


def should_log_hedge_fill_on_uncertain(sold, remaining) -> bool:
    """Wallet already lost the bag; confirm path never logged hedge_fill.

    Last-120 tape 27–31 Aug: 91 ``hedge_attempt``, 91 ``hedge_fail``,
    0 ``hedge_fill``, 52 ``hedge_uncertain_resolved`` matching 52 CSV
    sells. The inspect path must still emit ``hedge_fill`` so the tape
    matches the wallet. Same numeric gate as ``should_mark_hedge_closed``.
    """
    return should_mark_hedge_closed(sold, remaining)


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
    """Once holding, do not sell while last live BTC clearly favors the held leg.

    CLOB one-ticks and unreflective TOB are not a hedge if the last-print
    oracle still says the held leg wins. Missing/stale oracle also blocks
    the sell (fail closed against false hedges). A flipped or exactly-flat
    oracle lets the book path continue. For ``edge_too_small``, use the
    signed edge: still-agreeing (favorable) blocks; against allows. Near-PTB
    dumps that ignore oracle remain a separate override.
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
    reason = str(check.get("reason") or "")
    if reason == "edge_zero":
        return True, "oracle_flat"
    if reason == "edge_too_small":
        try:
            edge = float(check.get("edge_usd"))
        except (TypeError, ValueError):
            return False, "oracle_unknown"
        if not math.isfinite(edge):
            return False, "oracle_unknown"
        if edge == 0:
            return True, "oracle_flat"
        favored_small = "up" if edge > 0 else "down"
        if favored_small == leg:
            return False, "oracle_still_winning_small"
        return True, "oracle_against_small"
    return False, "oracle_unknown"


def hedge_dump_overrides_oracle(bid, dump_bid_max, *, enabled=True) -> bool:
    """True when a toxic CLOB dump should proceed even if BTC still agrees.

    Persist-50 sells stay behind the oracle. A 4–32¢ book is not a one-tick
    dip; blocking that dump is how the 20d tape missed losers.
    """
    if not enabled:
        return False
    bid_f = _finite_px(bid)
    if bid_f is None:
        return False
    try:
        dump_max = float(dump_bid_max)
    except (TypeError, ValueError):
        return False
    return bid_f <= dump_max + 1e-12


def hedge_flatten_overrides_oracle(bid, flatten_max, *, armed=False, enabled=True) -> bool:
    """True when a walk flatten should skip REST / ignore the oracle.

    Bid < flatten_max (live 75¢). Bid ≥ that is a recovered walk: ride.
    ``armed`` is ``toxic_fill``. Off unless both flags are set.
    """
    if not enabled or not armed:
        return False
    bid_f = _finite_px(bid)
    fmax = _finite_px(flatten_max)
    if bid_f is None or fmax is None:
        return False
    return bid_f + 1e-12 < fmax


def hedge_rest_required(*, persist_done, peek_dump) -> bool:
    """REST unless persist is already done or a fresh WS dump peek is enough.

    Stale last-good dump must not skip REST (false dump while BTC still agrees).
    """
    return not (bool(persist_done) or bool(peek_dump))


def hedge_oracle_blocks_sell(*, dump, oracle_agrees, dump_ignore_oracle=True) -> bool:
    """True → do not sell this tick.

    Dump never blocked when ``dump_ignore_oracle``. Persist-50 stays gated.
    """
    if dump and dump_ignore_oracle:
        return False
    return bool(oracle_agrees)



def blended_cost_per_share(pnl_entry_cost, bought_size, fill_price=None):
    """Blended VWAP: pnl_entry_cost / bought_size, else fill_price fallback."""
    try:
        size = float(bought_size or 0)
    except (TypeError, ValueError):
        size = 0.0
    try:
        cost = float(pnl_entry_cost or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if size > 1e-12 and cost > 0:
        return cost / size
    return _finite_px(fill_price)


def take_profit_market_armed(market_start_ts, from_start_ts) -> bool:
    """True when take-profit may apply to this market window.

    from_start_ts <= 0 means all markets (no grandfather floor).
    Live deploy sets from_start_ts to 10:00 America/New_York 2026-09-07
    (= 1788789600) so the open 9AM ET bag (start 1788786000) is excluded.
    """
    try:
        floor = float(from_start_ts or 0)
    except (TypeError, ValueError):
        floor = 0.0
    if floor <= 1e-12:
        return True
    try:
        start = float(market_start_ts)
    except (TypeError, ValueError):
        return False
    return start + 1e-12 >= floor


def take_profit_ready(bid, vwap, edge) -> bool:
    """True when live bid >= blended cost + edge (e.g. vwap 0.947 -> bid >= 0.987)."""
    bid_f = _finite_px(bid)
    vwap_f = _finite_px(vwap)
    if bid_f is None or vwap_f is None:
        return False
    try:
        edge_f = float(edge)
    except (TypeError, ValueError):
        return False
    if edge_f < 0:
        return False
    return bid_f + 1e-12 >= vwap_f + edge_f




def take_profit_full_ready(bid, full_bid) -> bool:
    """True when live bid is locked at/above the full-exit threshold.

    Polymarket UI often shows ~99.9¢ while the 0.01-tick book prints bid
    0.99 / ask 0.999. Default full_bid 0.999 means: if bid holds at/above 99.9¢, sell the whole bag — including leftover after a half TP.
    full_bid <= 0 disables the lock path.
    """
    bid_f = _finite_px(bid)
    if bid_f is None:
        return False
    try:
        lock = float(full_bid)
    except (TypeError, ValueError):
        return False
    if lock <= 1e-12:
        return False
    if lock > 1.0:
        return False
    return bid_f + 1e-12 >= lock


def take_profit_sell_size(held_size, fraction=0.5) -> float:
    """Shares to FAK on take-profit: fraction of bag, CLOB 2dp ROUND_DOWN.

    Returns 0.0 when the rounded size is below one cent-share (0.01), so
    callers can skip rather than posting a dust order. Default fraction 0.5
    locks half the bag and leaves the rest for resolution / hedge / dump.
    """
    try:
        held = float(held_size or 0)
    except (TypeError, ValueError):
        return 0.0
    try:
        frac = float(fraction)
    except (TypeError, ValueError):
        return 0.0
    if held <= 1e-12 or frac <= 1e-12:
        return 0.0
    if frac > 1.0:
        frac = 1.0
    raw = Decimal(str(held)) * Decimal(str(frac))
    shares = raw.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if shares < Decimal("0.01"):
        return 0.0
    out = float(shares)
    if out > held + 1e-12:
        out = float(
            Decimal(str(held)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        )
    return out


def take_profit_overrides_oracle(
    bid,
    vwap,
    edge,
    *,
    enabled=True,
    market_start_ts=None,
    from_start_ts=0.0,
) -> bool:
    """True when a take-profit sell should proceed even if oracle still agrees.

    Same spirit as hedge_dump_overrides_oracle / toxic flatten: book + cost
    edge alone is enough; do not wait for BTC to flip against a winning bag.
    """
    if not enabled:
        return False
    if not take_profit_market_armed(market_start_ts, from_start_ts):
        return False
    return take_profit_ready(bid, vwap, edge)


def held_hedge_decision(
    rest_bid,
    rest_ask,
    ws_bid,
    ws_ask,
    last_bid,
    last_ask,
    *,
    now_s,
    persist_armed_ts,
    persist_done=False,
    oracle_agrees=False,
    dump_ignore_oracle=True,
    dump_bid_max=0.32,
    qualify_bid=0.50,
    qualify_ask_max=0.52,
    recovery_cancel=0.53,
    persist_s=5.0,
    sell_fade=True,
    max_spread=0.15,
    gui_ok=True,
    gui_why="ok",
    flatten=False,
    flatten_max=None,
    dump_persist_s=0.0,
    dump_armed_ts=None,
    seconds_left=None,
    late_ttm=0.0,
    late_dump=0.40,
    late_qualify=0.58,
    late_ask_max=0.60,
    late_recovery=0.62,
    dump_require_tight=False,
    dump_ignore_spread_ask_max=0.60,
):
    """REST + pick + evaluate + oracle block for one held tick (no I/O).

    Fresh WS dump/flatten peek (or persist_done) may skip REST. Last-good
    is only a pick fallback after REST is allowed to run. Dump and flatten
    skip the oracle; a persist sell does not.

    Pass ``seconds_left`` to raise persist/recovery in the last
    ``late_ttm`` seconds (5m). Dump stays on the early floor unless
    ``late_dump`` is set higher. ``late_ttm`` ≤ 0 or omitted TTM keeps
    the static early knobs. Hourly must not pass 5m seconds.
    """
    if seconds_left is not None:
        ladder = hedge_ladder_for_ttm(
            seconds_left,
            dump_bid_max,
            qualify_bid,
            qualify_ask_max,
            recovery_cancel,
            late_ttm=late_ttm,
            late_dump=late_dump,
            late_qualify=late_qualify,
            late_ask_max=late_ask_max,
            late_recovery=late_recovery,
        )
        dump_bid_max = ladder.dump
        qualify_bid = ladder.qualify
        qualify_ask_max = ladder.ask_max
        recovery_cancel = ladder.recovery
    peek_dump = hedge_dump_overrides_oracle(ws_bid, dump_bid_max, enabled=True)
    peek_flatten = hedge_flatten_overrides_oracle(
        ws_bid, flatten_max, armed=flatten, enabled=True,
    )
    fetch_rest = hedge_rest_required(
        persist_done=persist_done, peek_dump=peek_dump or peek_flatten,
    )
    use_rest_bid = rest_bid if fetch_rest else None
    use_rest_ask = rest_ask if fetch_rest else None
    bid, ask = pick_held_quote(
        use_rest_bid, use_rest_ask, ws_bid, ws_ask, last_bid, last_ask,
    )
    intent = evaluate_held_bag(
        bid, ask,
        now_s=now_s,
        persist_armed_ts=persist_armed_ts,
        persist_s=persist_s,
        dump_bid_max=dump_bid_max,
        qualify_bid=qualify_bid,
        qualify_ask_max=qualify_ask_max,
        max_spread=max_spread,
        persist_done=persist_done,
        gui_ok=gui_ok,
        gui_why=gui_why,
        recovery_cancel=recovery_cancel,
        sell_fade=sell_fade,
        flatten=flatten,
        flatten_max=flatten_max,
        dump_persist_s=dump_persist_s,
        dump_armed_ts=dump_armed_ts,
        dump_require_tight=dump_require_tight,
        dump_ignore_spread_ask_max=dump_ignore_spread_ask_max,
    )
    if hedge_oracle_blocks_sell(
        dump=bool(intent.dump),
        oracle_agrees=oracle_agrees,
        dump_ignore_oracle=dump_ignore_oracle,
    ):
        return HedgeIntent(
            "hold", "oracle_still_winning", None, None, False, True, None, False,
        )
    return intent

def dump_exit_best_reasonable(meta, ladder_start: float) -> float:
    """Prefer a recent recovery/tight bid logged on meta; else ladder_start."""
    try:
        start = float(ladder_start)
    except (TypeError, ValueError):
        start = 0.55
    if start != start or start <= 0:
        start = 0.55
    if not isinstance(meta, dict):
        return start
    for key in (
        "dump_ladder_tight_bid",
        "last_tight_bid",
        "hedge_tight_bid",
        "last_good_bid",
    ):
        raw = meta.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value == value and 0.0 < value <= 1.0:
            return value
    return start


def align_dump_sell_limit(price, tick) -> float:
    """Tick-align a SELL FAK limit downward; never below one tick."""
    try:
        tick_f = float(tick)
    except (TypeError, ValueError):
        tick_f = 0.01
    if tick_f <= 0:
        tick_f = 0.01
    try:
        raw = float(price or 0)
    except (TypeError, ValueError):
        raw = 0.0
    if raw != raw or raw <= 0:
        return tick_f
    aligned = (int(raw / tick_f + 1e-12)) * tick_f
    return max(tick_f, round(aligned, 10))


def build_dump_exit_price_ladder(
    live_bid,
    live_ask=None,
    *,
    tick=0.01,
    ladder_start=0.55,
    price_floor=0.45,
    step=0.05,
    late_sweep_ttm_s=45.0,
    ttm_s=None,
    meta=None,
    enabled=True,
):
    """Descending SELL FAK limits for toxic DUMP exits.

    Always HIGH→LOW from ``ladder_start`` toward ``max(live_bid, tick)``.
    Prefer spending time at ≥ ``price_floor`` first (same start/step).
    If still unfilled after the floor, keep stepping by ``step`` down to
    live bid even when ``live_bid < price_floor`` (do not require late TTM).
    When very late (``ttm <= late_sweep_ttm_s``), skip straight to
    ``max(live_bid, tick)``.
    """
    if not enabled:
        return []
    try:
        tick_f = float(tick) if tick not in (None, "") else 0.01
    except (TypeError, ValueError):
        tick_f = 0.01
    if tick_f <= 0:
        tick_f = 0.01
    bid = None
    if live_bid is not None:
        try:
            bid = float(live_bid)
        except (TypeError, ValueError):
            bid = None
        if bid is not None and (bid != bid or bid <= 0):
            bid = None
    ask = None
    if live_ask is not None:
        try:
            ask = float(live_ask)
        except (TypeError, ValueError):
            ask = None
        if ask is not None and (ask != ask or ask <= 0):
            ask = None
    try:
        start_cfg = float(ladder_start)
    except (TypeError, ValueError):
        start_cfg = 0.55
    try:
        floor = float(price_floor)
    except (TypeError, ValueError):
        floor = 0.45
    try:
        step_f = float(step)
    except (TypeError, ValueError):
        step_f = 0.05
    if step_f <= 0:
        step_f = tick_f
    best = dump_exit_best_reasonable(meta, start_cfg)
    seed = min(start_cfg, best)
    start = max(bid, seed) if bid is not None else seed
    # SELL FAK limit is a min-accept price: intentionally try ABOVE the live
    # bid first (may not fill). Do not clamp to ask-tick — that collapsed the
    # seek-high ladder whenever ask sat under ladder_start.
    _ = ask  # retained for call-site / future book-aware starts
    start = align_dump_sell_limit(start, tick_f)
    floor_al = align_dump_sell_limit(floor, tick_f)
    bottom = (
        align_dump_sell_limit(max(bid, tick_f), tick_f)
        if bid is not None
        else floor_al
    )

    late = False
    try:
        if ttm_s is not None and float(late_sweep_ttm_s) > 0:
            late = float(ttm_s) <= float(late_sweep_ttm_s) + 1e-12
    except (TypeError, ValueError):
        late = False

    # Very late: skip straight to live bid (optional fast sweep).
    if late and bid is not None:
        return [round(bottom, 10)]

    # Prefer ≥ floor first: never start below floor on the seek-high path.
    if start + 1e-12 < floor_al:
        start = floor_al

    # Ladder bottoms at max(live_bid, tick) when known; else stop at floor.
    stop_at = bottom if bid is not None else floor_al
    if stop_at + 1e-12 > start:
        stop_at = start

    levels = []
    p = start
    for _ in range(64):
        levels.append(round(p, 10))
        if p <= stop_at + 1e-12:
            break
        nxt = align_dump_sell_limit(p - step_f, tick_f)
        if nxt + 1e-12 >= p:
            break
        if nxt + 1e-12 < stop_at:
            if abs(levels[-1] - stop_at) > 1e-12:
                levels.append(round(stop_at, 10))
            break
        p = nxt

    out = []
    for lv in levels:
        if not out or abs(out[-1] - lv) > 1e-12:
            out.append(lv)
    return out

