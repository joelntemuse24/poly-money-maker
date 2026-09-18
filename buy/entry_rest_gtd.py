"""5m last-120s hybrid GTD rest helpers (pure; no bot import).

FAK-take when a real ask sits at the GUI tick. Otherwise rest one GTD bid
on that tick until the market ``end_ts``. Hedge/sell/redeem stay FAK.
"""

from __future__ import annotations

import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping, NamedTuple, Optional, Sequence

EPS = 1e-12
REST_TICKS = (0.97, 0.98, 0.99)
CLOB_GTD_MIN_LEAD_S = 180
REST_SNAP_MAX_ABS = 0.005
LOSER_GUI_MAX = 0.05
REST_META_KEYS = (
    "rest_gtd_order_id",
    "rest_gtd_price",
    "rest_gtd_leg",
    "rest_gtd_token",
    "rest_gtd_expiration",
    "rest_gtd_size",
    "rest_gtd_baseline",
)


class HybridIntent(NamedTuple):
    action: str
    why: str
    leg: Optional[str] = None
    tick: Optional[float] = None
    token: Optional[str] = None
    expiration: Optional[int] = None
    cancel_order_id: Optional[str] = None
    fak_limit: Optional[float] = None
    fak_min: Optional[float] = None


def _finite(value: Any, *, lo: float = 0.0, hi: float = 1.0) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed < lo - EPS or parsed > hi + EPS:
        return None
    return parsed


def snap_gui_to_rest_tick(
    gui,
    ticks: Sequence[float] = REST_TICKS,
    max_abs: float = REST_SNAP_MAX_ABS,
) -> Optional[float]:
    """Nearest unique rest tick if ``|gui − tick| ≤ max_abs``.

    Equal distance (0.975 / 0.985) skips. GUI 0.96 / 1.00 skip.
    """
    price = _finite(gui)
    if price is None:
        return None
    best: list[tuple[float, float]] = []
    for raw in ticks:
        tick = _finite(raw)
        if tick is None:
            continue
        dist = abs(price - tick)
        if dist <= float(max_abs) + EPS:
            best.append((dist, tick))
    if not best:
        return None
    best.sort(key=lambda item: (item[0], item[1]))
    if len(best) >= 2 and abs(best[0][0] - best[1][0]) <= EPS:
        return None
    return float(best[0][1])


def _at_tick(price, tick, *, tol: float = 1e-9) -> bool:
    px = _finite(price)
    tk = _finite(tick)
    if px is None or tk is None:
        return False
    return abs(px - tk) <= tol + EPS


def rest_winner_leg(
    *,
    up_gui,
    dn_gui,
    up_bid=None,
    up_ask=None,
    up_last=None,
    dn_bid=None,
    dn_ask=None,
    dn_last=None,
    ticks: Sequence[float] = REST_TICKS,
    max_abs: float = REST_SNAP_MAX_ABS,
    loser_gui_max: float = LOSER_GUI_MAX,
) -> tuple[Optional[str], Optional[float], str]:
    """Unique 97–99 winner. Both-in-band or a 0.975 tie → skip."""
    up_tick = snap_gui_to_rest_tick(up_gui, ticks=ticks, max_abs=max_abs)
    dn_tick = snap_gui_to_rest_tick(dn_gui, ticks=ticks, max_abs=max_abs)
    if up_tick is not None and dn_tick is not None:
        return None, None, "ambiguous"
    if up_tick is None and dn_tick is None:
        return None, None, "skip_gui"

    def _winner(leg: str, tick: float, own_bid, own_last, other_gui) -> tuple[Optional[str], Optional[float], str]:
        other = _finite(other_gui)
        own_ok = _at_tick(own_bid, tick) or _at_tick(own_last, tick)
        if other is not None:
            if other > float(loser_gui_max) + EPS:
                return None, None, "ambiguous"
            return leg, tick, "ok"
        if own_ok:
            return leg, tick, "ok"
        return None, None, "no_winner"

    if up_tick is not None:
        return _winner("up", up_tick, up_bid, up_last, dn_gui)
    return _winner("down", dn_tick, dn_bid, dn_last, up_gui)


def ask_allows_fak_take(ask, tick, band_min: float = REST_TICKS[0]) -> bool:
    """True when a real ask sits at the GUI tick, or ≤ tick and ≥ 0.97."""
    px = _finite(ask)
    tk = _finite(tick)
    lo = _finite(band_min)
    if px is None or tk is None or lo is None:
        return False
    if px > tk + EPS:
        return False
    return px + EPS >= lo


def rest_persist_eligible(
    *,
    tick,
    bid=None,
    ask=None,
    last=None,
    gui=None,
) -> bool:
    """Arm persist from WS GUI/bid/last at the tick. Missing ask is OK."""
    _ = ask  # REST missing ask must not reset.
    tk = _finite(tick)
    if tk is None:
        return False
    return any(_at_tick(px, tk) for px in (bid, last, gui))


def rest_maker_shares(
    budget,
    tick,
    share_cap=None,
    min_notional: float = 1.0,
) -> float:
    """Shares such that ``shares × tick`` is exact 2dp USDC and ≥ $1."""
    try:
        spend = Decimal(str(budget)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        tick_d = Decimal(str(tick))
    except Exception:
        return 0.0
    if spend < Decimal("0.01") or tick_d <= 0:
        return 0.0
    share_tick = Decimal("0.01")
    min_shares = Decimal("0.01")
    cent = Decimal("0.01")
    floor = Decimal(str(min_notional))
    shares = (spend / tick_d).quantize(share_tick, rounding=ROUND_DOWN)
    if share_cap is not None:
        try:
            shares = min(
                shares,
                Decimal(str(share_cap)).quantize(share_tick, rounding=ROUND_DOWN),
            )
        except Exception:
            pass
    while shares >= min_shares:
        maker = shares * tick_d
        if maker.quantize(cent) == maker and maker + Decimal("0") >= floor:
            if maker == Decimal("1.01"):
                shares -= share_tick
                shares = shares.quantize(share_tick, rounding=ROUND_DOWN)
                continue
            return float(shares)
        shares -= share_tick
        shares = shares.quantize(share_tick, rounding=ROUND_DOWN)
    return 0.0


def unix_ts(value) -> Optional[int]:
    """Parse a positive unix-seconds timestamp. No CLOB floor."""
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return ts


def gtd_expiration(
    end_ts,
    now=None,
    min_lead_s: int = CLOB_GTD_MIN_LEAD_S,
) -> Optional[int]:
    """GTD unix seconds for CLOB POST.

    Prefer this market's ``end_ts``. CLOB rejects expiration < now+180, and
    the last-120s window is always inside that floor, so we lift to
    ``now + min_lead_s``. The bot still cancels this ``order_id`` at TTM<=0
    / window close; CLOB expiry is only the backstop if cancel fails. Next
    5m uses different tokens.
    """
    ts = unix_ts(end_ts)
    if ts is None:
        return None
    lead = int(min_lead_s)
    if lead <= 0:
        return ts
    try:
        now_s = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        now_s = time.time()
    floor = int(now_s) + lead
    return ts if ts >= floor else floor


def rest_gtd_post_is_rejected(error) -> bool:
    """True when CLOB refused the GTD (never booked). Do not keep/uncertain."""
    text = str(error or "").lower()
    if not text:
        return False
    if "expiration is less than" in text:
        return True
    if "invalid expiration" in text:
        return True
    return False


def live_rest_from_meta(meta: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if not meta:
        return None
    order_id = str(meta.get("rest_gtd_order_id") or "").strip()
    if not order_id:
        return None
    price = _finite(meta.get("rest_gtd_price"))
    expiration = unix_ts(meta.get("rest_gtd_expiration"))
    token = meta.get("rest_gtd_token")
    leg = str(meta.get("rest_gtd_leg") or "").strip().lower() or None
    return {
        "order_id": order_id,
        "price": price,
        "leg": leg,
        "token": None if token is None else str(token),
        "expiration": expiration,
        "size": _finite(meta.get("rest_gtd_size"), hi=10**9),
    }


def persist_rest_meta(
    meta: dict,
    *,
    order_id: str,
    price,
    leg: str,
    token,
    expiration,
    size=None,
    baseline=None,
) -> None:
    meta["rest_gtd_order_id"] = str(order_id)
    meta["rest_gtd_price"] = float(price)
    meta["rest_gtd_leg"] = str(leg)
    meta["rest_gtd_token"] = str(token)
    meta["rest_gtd_expiration"] = int(expiration)
    if size is not None:
        meta["rest_gtd_size"] = float(size)
    if baseline is not None:
        meta["rest_gtd_baseline"] = float(baseline)


def clear_rest_meta(meta: Optional[dict]) -> None:
    if not meta:
        return
    for key in REST_META_KEYS:
        meta.pop(key, None)


def rest_fill_state(status, *, size_matched=0.0, token_delta=0.0) -> str:
    """filled | live | dead_empty | unknown"""
    matched = 0.0
    delta = 0.0
    try:
        matched = float(size_matched or 0)
    except (TypeError, ValueError):
        matched = 0.0
    try:
        delta = float(token_delta or 0)
    except (TypeError, ValueError):
        delta = 0.0
    if matched > 0.01 or delta > 0.01:
        return "filled"
    label = str(status or "").strip().lower()
    if label in {"live", "open", "booked", "live_order", "order_status_live"}:
        return "live"
    if any(part in label for part in ("cancel", "expir", "invalid", "reject", "unmatch", "not_found", "dead")):
        return "dead_empty"
    if label in {"matched", "order_status_matched"} and matched <= 0.01:
        return "unknown"
    return "unknown"


def book_level_kept_for_display(price) -> bool:
    """$1.00 may show; rest ticks never include it."""
    px = _finite(price, lo=0.0, hi=1.0)
    return px is not None and 0 < px <= 1 + EPS


def hybrid_late_intent(
    *,
    live: Optional[Mapping[str, Any]],
    up_gui,
    dn_gui,
    up_bid=None,
    up_ask=None,
    up_last=None,
    dn_bid=None,
    dn_ask=None,
    dn_last=None,
    up_token=None,
    dn_token=None,
    seconds_left=0.0,
    late_start_s=120.0,
    end_ts=None,
    enabled: bool = False,
    already_filled: bool = False,
    ticks: Sequence[float] = REST_TICKS,
    now=None,
) -> HybridIntent:
    """Keep / replace / cancel / FAK / rest / skip for one 5m market."""
    live_id = None
    live_price = None
    live_leg = None
    if live:
        live_id = str(live.get("order_id") or "").strip() or None
        live_price = _finite(live.get("price"))
        live_leg = str(live.get("leg") or "").strip().lower() or None

    def _cancel(why: str) -> HybridIntent:
        return HybridIntent(action="cancel", why=why, cancel_order_id=live_id)

    if not enabled:
        if live_id:
            return _cancel("knob_off")
        return HybridIntent(action="skip", why="knob_off")

    try:
        ttm = float(seconds_left)
        window = float(late_start_s)
    except (TypeError, ValueError):
        return _cancel("invalid_ttm") if live_id else HybridIntent(action="skip", why="invalid_ttm")
    if ttm <= 0 or ttm > window + EPS:
        if live_id:
            return _cancel("window_closed")
        return HybridIntent(action="skip", why="window_closed")

    if already_filled:
        if live_id:
            return _cancel("already_filled")
        return HybridIntent(action="skip", why="already_filled")

    expiration = gtd_expiration(end_ts, now=now)
    leg, tick, why = rest_winner_leg(
        up_gui=up_gui,
        dn_gui=dn_gui,
        up_bid=up_bid,
        up_ask=up_ask,
        up_last=up_last,
        dn_bid=dn_bid,
        dn_ask=dn_ask,
        dn_last=dn_last,
        ticks=ticks,
    )
    if leg is None or tick is None:
        if live_id:
            return _cancel(why)
        return HybridIntent(action="skip", why=why)

    token = up_token if leg == "up" else dn_token
    ask = up_ask if leg == "up" else dn_ask
    if live_id:
        if live_leg and live_leg != leg:
            return _cancel("became_loser")
        if live_price is not None and abs(live_price - tick) <= EPS:
            return HybridIntent(
                action="keep",
                why="same_tick",
                leg=leg,
                tick=tick,
                token=None if token is None else str(token),
                expiration=expiration,
                cancel_order_id=None,
            )
        return HybridIntent(
            action="replace",
            why="tick_moved",
            leg=leg,
            tick=tick,
            token=None if token is None else str(token),
            expiration=expiration,
            cancel_order_id=live_id,
        )

    if ask_allows_fak_take(ask, tick):
        return HybridIntent(
            action="fak",
            why="ask_at_tick",
            leg=leg,
            tick=tick,
            token=None if token is None else str(token),
            expiration=expiration,
            fak_limit=float(tick),
            fak_min=float(ticks[0]),
        )
    return HybridIntent(
        action="rest",
        why="no_ask_at_tick",
        leg=leg,
        tick=tick,
        token=None if token is None else str(token),
        expiration=expiration,
    )
