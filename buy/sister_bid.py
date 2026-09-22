"""Sister scrap-bidder policy (wallet B). No CLOB posts, no mintbot import.

Wallet A (mintbot) mints and sells. Wallet B buys the sold leg. Once A
has ``sold_loser`` on leg L, B FAK-buys that leg at the live ask, up to
``bid_fak_max_notional / shares`` (20 shares → 7.5¢, $1.50). If
``shares * ask`` is under ``bid_fak_min_notional`` ($1), the FAK limit
is raised to ``bid_fak_min_notional / shares`` (5¢) so the marketable
BUY clears $1; the fill is still the cheaper ask. FAK only when the ask
is at or under that max and ``limit * shares`` sits in [$1.00, $1.50].
If the take cannot fire, B rests a GTD/GTC BUY at ``bid_rest_px`` (5¢),
and only when that rest is strictly below the ask. A rest at or above
the ask would be marketable and get rejected under $1. GTD is used when
expiration is at least ~180s ahead; otherwise GTC, still cancelled near
expiry. There is no escalate ladder. Markets A never held use the same
quote on the cheap live side during the late window. The winner leg A
still holds stays blocked. B never mints and never sells.

Same-wallet buyback is intentionally not here.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from buy.mint_sell import resting_tif

# Public addresses. Not secrets. Wallet A must never be the bidder funder.
MINTBOT_FUNDER = "0x822279ae008c54b7ab4dd733994c76a711258b4e"
COMPLEMENT_DEPOSIT = "0x2b2D1dA1a49E8BF73EbBC3EAC35D79cc88cd4ad2"
COMPLEMENT_PROXY = "0xCfF52577f80222e4b36f03B5d58443781b9D2433"
# py_clob_client_v2 SignatureTypeV2.POLY_1271
POLY_1271 = 3

SISTER_DEFAULTS = {
    "bid_enabled": False,
    "dry_run": True,
    "shares": 20.0,
    "bid_max_px": 0.05,
    # Passive rest. The FAK ceiling is bid_fak_max_notional / shares (7.5¢).
    "bid_rest_px": 0.05,
    # 20sh × 5¢ = $1.00 floor. 20sh × 7.5¢ = $1.50 max.
    "bid_fak_min_notional": 1.0,
    "bid_fak_max_notional": 1.5,
    # FAK the live ask when it is ≤ max notional / shares.
    "bid_take_enabled": True,
    "active_ttm_s": 180.0,
    "cancel_ttm_s": 20.0,
    # Polymarket rejects a GTD that expires inside ~180s. Shorter → GTC.
    "min_gtd_ahead_s": 180.0,
    "poll_s": 2.0,
    # Faster cadence while a sold loser has no resting B bid.
    "poll_hot_s": 1.0,
    # Loud miss if B has not bid within this many seconds of A going flat.
    "miss_after_s": 10.0,
    "miss_throttle_s": 30.0,
    "series_slugs": ["btc-up-or-down-15m"],
    "signature_type": POLY_1271,
}

_HELD_STATUSES = frozenset(
    {
        "submitting",
        "pending",
        "executed",
        "mined",
        "confirmed_waiting_inventory",
        "confirmed",
    }
)


def sister_funder_ok(funder: str) -> tuple[bool, str]:
    """Reject the live mintbot funder so B cannot sign as A."""
    text = str(funder or "").strip()
    if not text:
        return False, "missing_funder"
    if text.lower() == MINTBOT_FUNDER.lower():
        return False, "mintbot_funder"
    return True, "ok"


def resolve_sister_client_config(env: Optional[dict]) -> dict:
    """CLOB client settings from the complement env. Never returns a key."""
    src = env or {}
    funder = str(src.get("FUNDER_ADDRESS") or COMPLEMENT_DEPOSIT).strip()
    ok, why = sister_funder_ok(funder)
    if not ok:
        raise ValueError(why)
    raw_sig = src.get("SIGNATURE_TYPE", POLY_1271)
    try:
        signature_type = int(raw_sig)
    except (TypeError, ValueError) as exc:
        raise ValueError("bad_signature_type") from exc
    try:
        chain_id = int(src.get("CHAIN_ID") or 137)
    except (TypeError, ValueError) as exc:
        raise ValueError("bad_chain_id") from exc
    return {
        "funder": funder,
        "signature_type": signature_type,
        "host": "https://clob.polymarket.com",
        "chain_id": chain_id,
    }


def _sold_leg(intent: dict) -> Optional[str]:
    """Leg A has already scrapped, if the intent says which one."""
    sold_leg = intent.get("sold_leg") if intent.get("sold_leg") in ("up", "dn") else None
    sold_flag = bool(intent.get("sold_loser") or sold_leg)
    if not sold_flag:
        return None
    if sold_leg in ("up", "dn"):
        return sold_leg
    loser = intent.get("sell_loser_leg")
    if loser in ("up", "dn"):
        return loser
    return None


def a_token_flat(intent: Any, leg: str) -> tuple[bool, str]:
    """Whether wallet B may bid ``leg``.

    ``sold_loser`` / ``sold_leg`` on L makes L flat immediately. A's
    ``sell_scrap_rest_id`` does not block that leg: the two wallets are
    different, and A's resting sell into B's bid is the hedge. The other
    leg is the winner A still holds. A rest with no sold flag still means
    A is long (``a_rest_live``). A missing intent, or a non-held status
    with no sold flag, is ``a_absent``. ``sold_loser`` / ``sold_leg`` stay
    ``a_flat`` even when status is ``completed``.
    """
    if leg not in ("up", "dn"):
        return False, "bad_leg"
    if not isinstance(intent, dict):
        return True, "a_absent"
    # Sold flags win over status. ``completed`` is not a held status, but a
    # loser A already sold is still the post-scrap leg while the window is open.
    sold_leg = _sold_leg(intent)
    if sold_leg == leg:
        return True, "a_flat"
    if sold_leg in ("up", "dn") and sold_leg != leg:
        return False, "a_holds_other"
    status = str(intent.get("status") or "")
    if status not in _HELD_STATUSES:
        return True, "a_absent"
    rest_id = intent.get("sell_scrap_rest_id")
    rest_leg = intent.get("sell_loser_leg") if intent.get("sell_loser_leg") in ("up", "dn") else None
    if rest_id and (rest_leg is None or rest_leg == leg):
        return False, "a_rest_live"
    return False, "a_still_long"


def _has_open_order(open_orders: Optional[dict], cid: str) -> bool:
    slot = (open_orders or {}).get(cid) or {}
    if not isinstance(slot, dict):
        return False
    for leg in ("up", "dn"):
        order = slot.get(leg)
        if isinstance(order, dict) and str(order.get("order_id") or ""):
            return True
    return False


def sister_book_wanted(
    market: Any,
    intent: Any,
    open_orders: Optional[dict],
    *,
    now_s: float,
    active_ttm_s: float = 180.0,
    cancel_ttm_s: float = 20.0,
) -> bool:
    """Whether this pass should read the public book for ``market``.

    Far-future windows are not quoted. A book is needed for an open sister
    order, a sold leg still before cancel, or any market inside the late
    ``active_ttm_s`` window.
    """
    if not isinstance(market, dict):
        return False
    cid = str(market.get("condition_id") or "")
    if cid and _has_open_order(open_orders, cid):
        return True
    try:
        end_ts = float(market.get("end_ts") or 0)
    except (TypeError, ValueError):
        end_ts = 0.0
    if end_ts <= 0:
        return isinstance(intent, dict) and _sold_leg(intent) is not None
    ttm = end_ts - float(now_s)
    if ttm <= float(cancel_ttm_s) + 1e-12:
        return False
    if isinstance(intent, dict) and _sold_leg(intent) is not None:
        return True
    return ttm <= float(active_ttm_s) + 1e-12


def markets_needing_books(
    markets: Sequence[dict],
    intents: Optional[dict],
    open_orders: Optional[dict],
    *,
    now_s: float,
    active_ttm_s: float = 180.0,
    cancel_ttm_s: float = 20.0,
) -> list[dict]:
    """Markets whose book can change this pass. Pure; no HTTP."""
    intents = intents or {}
    chosen: list[dict] = []
    for market in markets or []:
        if not isinstance(market, dict):
            continue
        cid = str(market.get("condition_id") or "")
        intent = intents.get(cid) if cid else None
        if sister_book_wanted(
            market,
            intent,
            open_orders,
            now_s=now_s,
            active_ttm_s=active_ttm_s,
            cancel_ttm_s=cancel_ttm_s,
        ):
            chosen.append(market)
    return chosen


def sister_cancel_due(
    *, ttm_s: Optional[float], cancel_ttm_s: float, window_open: bool
) -> tuple[bool, str]:
    """Cancel a resting sister bid at T−cancel or when the window is over."""
    if not window_open or ttm_s is None or float(ttm_s) <= 0:
        return True, "window_end"
    if float(ttm_s) <= float(cancel_ttm_s) + 1e-12:
        return True, "cancel_before_expiry"
    return False, "keep"


def _leg_filled(filled: Optional[dict], cid: str, leg: str) -> float:
    slot = (filled or {}).get(cid) or {}
    if not isinstance(slot, dict):
        return 0.0
    try:
        shares = float(slot.get(leg) or 0)
    except (TypeError, ValueError):
        return 0.0
    if shares != shares or shares <= 0:
        return 0.0
    return shares


def sister_leg_filled(filled: Any, condition_id: str, leg: str) -> float:
    """Shares wallet B has matched on this condition and leg.

    ``filled`` is the ``filled`` map from ``positions_scrapbid.json``.
    """
    if not isinstance(filled, dict):
        return 0.0
    return _leg_filled(filled, str(condition_id or ""), str(leg or ""))


def buy_matched_shares(result: Any, offered: float) -> float:
    """Shares bought from a BUY POST or get_order.

    ``takingAmount`` / ``size_matched`` are the share leg. ``makingAmount``
    is USDC paid and must not be counted as shares.
    """
    if not isinstance(result, dict):
        return 0.0
    offered_f = float(offered or 0)
    for key in (
        "size_matched",
        "sizeMatched",
        "matched",
        "takingAmount",
        "taking_amount",
    ):
        raw = result.get(key)
        if raw is None or raw == "":
            continue
        try:
            human = float(raw)
        except (TypeError, ValueError):
            continue
        if human != human or human <= 0:
            continue
        fixed = human / 1_000_000.0
        if offered_f > 0:
            value = human if abs(human - offered_f) <= abs(fixed - offered_f) else fixed
        else:
            value = fixed if human >= 10_000 else human
        if value > 0:
            return value
    return 0.0


def _px(value: Any) -> Optional[float]:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price != price or price <= 0 or price >= 1:
        return None
    return price


def sister_quote(
    *,
    bid: Any = None,
    ask: Any = None,
    bid_max_px: float = 0.05,
    bid_rest_px: float = 0.05,
    take_enabled: bool = True,
    shares: float = 20.0,
    fak_min_notional: float = 1.0,
    fak_max_notional: float = 1.5,
) -> tuple[str, float, str]:
    """Post-scrap price. ``(style, price, why)``.

    ``style`` is ``take`` (FAK buy), ``rest``, or ``wait``. The take limit
    is the live ask, clipped up to ``fak_min_notional / shares`` when that
    ask would print under $1, and never above ``fak_max_notional / shares``
    (20 shares → 5¢–7.5¢). A rest is ``bid_rest_px``, and only when it
    does not cross the ask.
    """
    del bid
    cap = round(float(bid_max_px or 0), 4)
    rest = round(float(bid_rest_px or cap), 4)
    if rest <= 0 or (cap > 0 and rest > cap + 1e-12):
        rest = cap
    ask_px = _px(ask)
    size = float(shares or 0)
    lo = float(fak_min_notional or 0)
    hi = float(fak_max_notional or 0)
    if take_enabled and ask_px is not None and size > 0 and lo > 0 and hi + 1e-12 >= lo:
        floor_px = lo / size
        ceil_px = hi / size
        if ask_px <= ceil_px + 1e-12 and floor_px <= ceil_px + 1e-12:
            limit = ask_px if ask_px + 1e-12 >= floor_px else floor_px
            limit = round(min(limit, ceil_px), 4)
            notional = size * limit
            if lo - 1e-9 <= notional <= hi + 1e-9:
                why = "live_ask" if abs(limit - round(ask_px, 4)) <= 1e-9 else "fak_floor"
                return "take", limit, why
    if rest <= 0:
        return "rest", 0.0, "bad_cap"
    if ask_px is not None and rest + 1e-12 >= ask_px:
        return "wait", 0.0, "cross_ask"
    return "rest", rest, "rest_cap"


def _cheap_live(bid: Optional[float], bid_max_px: float) -> bool:
    if bid is None:
        return False
    try:
        return float(bid) <= float(bid_max_px) + 1e-12
    except (TypeError, ValueError):
        return False


def plan_sister_bids(
    *,
    markets: Sequence[dict],
    intents: Optional[dict],
    open_orders: Optional[dict],
    now_s: float,
    shares: float = 20.0,
    bid_max_px: float = 0.05,
    bid_rest_px: float = 0.05,
    active_ttm_s: float = 180.0,
    cancel_ttm_s: float = 20.0,
    min_gtd_ahead_s: float = 180.0,
    fak_min_notional: float = 1.0,
    fak_max_notional: float = 1.5,
    enabled: bool = True,
    take_enabled: bool = True,
    filled_shares: Optional[dict] = None,
) -> list[dict]:
    """Resting-bid plan for one pass. Does not post.

    Each row is ``place``, ``cancel``, ``keep``, or ``skip``.
    """
    intents = intents or {}
    open_orders = open_orders or {}
    actions: list[dict] = []
    size = float(shares or 0)
    px = round(float(bid_max_px or 0), 4)
    rest_px = round(float(bid_rest_px or 0), 4)
    for market in markets:
        if not isinstance(market, dict):
            continue
        cid = str(market.get("condition_id") or "")
        if not cid:
            continue
        end_ts = float(market.get("end_ts") or 0)
        ttm = (end_ts - float(now_s)) if end_ts else None
        window_open = ttm is not None and ttm > 0
        intent = intents.get(cid)
        orders = open_orders.get(cid) or {}
        if not isinstance(orders, dict):
            orders = {}
        bids = {"up": market.get("up_bid"), "dn": market.get("dn_bid")}
        asks = {"up": market.get("up_ask"), "dn": market.get("dn_ask")}
        tokens = {"up": market.get("up_token"), "dn": market.get("dn_token")}
        for leg in ("up", "dn"):
            flat, flat_why = a_token_flat(intent, leg)
            order = orders.get(leg) if isinstance(orders.get(leg), dict) else None
            order_id = str(order.get("order_id") or "") if order else ""
            cancel, cancel_why = sister_cancel_due(
                ttm_s=ttm, cancel_ttm_s=cancel_ttm_s, window_open=window_open
            )
            base = {
                "condition_id": cid,
                "leg": leg,
                "slug": market.get("slug"),
                "token_id": tokens.get(leg),
                "ttm_s": ttm,
            }
            if order_id and (not enabled or cancel or not flat):
                reason = "disabled" if not enabled else (
                    cancel_why if cancel else flat_why
                )
                actions.append({**base, "op": "cancel", "reason": reason, "order_id": order_id})
                continue
            if order_id:
                actions.append({**base, "op": "keep", "reason": "resting", "order_id": order_id})
                continue
            if not enabled:
                actions.append({**base, "op": "skip", "reason": "disabled"})
                continue
            if not window_open:
                actions.append({**base, "op": "skip", "reason": "window_end"})
                continue
            if not flat:
                actions.append({**base, "op": "skip", "reason": flat_why})
                continue
            # Post-scrap hedge does not wait for the last active_ttm_s.
            # Markets A never held still do.
            post_scrap = flat_why == "a_flat"
            if not post_scrap and (
                ttm is None or float(ttm) > float(active_ttm_s) + 1e-12
            ):
                actions.append({**base, "op": "skip", "reason": "too_early"})
                continue
            if cancel:
                actions.append({**base, "op": "skip", "reason": cancel_why})
                continue
            if flat_why == "a_absent" and not _cheap_live(bids.get(leg), px):
                actions.append({**base, "op": "skip", "reason": "not_cheap"})
                continue
            already = _leg_filled(filled_shares, cid, leg)
            remaining = round(max(0.0, size - already), 4)
            if size > 0 and remaining < 0.01:
                actions.append({**base, "op": "skip", "reason": "filled"})
                continue
            if remaining <= 0 or px <= 0 or not tokens.get(leg):
                actions.append({**base, "op": "skip", "reason": "bad_order"})
                continue
            # One cheap leg when A is absent: if both are cheap, bid the lower.
            if flat_why == "a_absent":
                other = "dn" if leg == "up" else "up"
                other_bid = bids.get(other)
                this_bid = bids.get(leg)
                if (
                    _cheap_live(other_bid, px)
                    and this_bid is not None
                    and other_bid is not None
                ):
                    if float(other_bid) + 1e-12 < float(this_bid):
                        actions.append({**base, "op": "skip", "reason": "other_cheaper"})
                        continue
                    if abs(float(other_bid) - float(this_bid)) <= 1e-12 and leg == "dn":
                        actions.append({**base, "op": "skip", "reason": "other_cheaper"})
                        continue
            style, price, price_why = sister_quote(
                bid=bids.get(leg),
                ask=asks.get(leg),
                bid_max_px=px,
                bid_rest_px=rest_px,
                take_enabled=bool(take_enabled),
                shares=remaining,
                fak_min_notional=float(fak_min_notional or 0),
                fak_max_notional=float(fak_max_notional or 0),
            )
            if style == "wait":
                actions.append({**base, "op": "skip", "reason": price_why})
                continue
            # Takes may pay up to max notional / size (7.5¢ at 20sh).
            # Rests stay at bid_rest_px, which is at or under bid_max_px.
            if style == "take" and remaining > 0 and float(fak_max_notional or 0) > 0:
                ceiling = float(fak_max_notional) / remaining
            else:
                ceiling = max(px, rest_px)
            if price <= 0 or price > ceiling + 1e-9:
                actions.append({**base, "op": "skip", "reason": "bad_order"})
                continue
            if style == "take":
                tif, exp, tif_why = "FAK", 0, "fak"
            else:
                tif, exp = resting_tif(
                    now_s=now_s,
                    expire_ts=float(end_ts) - float(cancel_ttm_s),
                    min_ahead_s=min_gtd_ahead_s,
                )
                tif_why = "gtd" if tif == "GTD" else "gtc_short_expiry"
            actions.append(
                {
                    **base,
                    "op": "place",
                    "reason": flat_why,
                    "price_why": price_why,
                    "style": style,
                    "price": price,
                    "shares": remaining,
                    "tif": tif,
                    "tif_why": tif_why,
                    "expiration": exp,
                }
            )
    return actions


def _finite_ts(value: Any) -> Optional[float]:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts != ts or ts <= 0:
        return None
    return ts


def flat_since_s(intent: Any, *, now_s: float, remembered: Any = None) -> float:
    """When B should start the miss clock for a sold leg.

    Prefer an explicit sold timestamp on the intent, then the FAK attempt
    clock mintbot already stores (``last_sell_attempt_at``). Otherwise the
    first time this process observed the leg flat.
    """
    now = float(now_s)
    candidates: list[float] = []
    if isinstance(intent, dict):
        for key in ("sold_loser_at", "sell_loser_done_at", "last_sell_attempt_at"):
            ts = _finite_ts(intent.get(key))
            if ts is not None and ts <= now + 1.0:
                candidates.append(ts)
    remembered_ts = _finite_ts(remembered)
    if remembered_ts is not None and remembered_ts <= now + 1.0:
        candidates.append(remembered_ts)
    if not candidates:
        return now
    return min(candidates)


def _order_id(open_orders: Optional[dict], cid: str, leg: str) -> str:
    slot = (open_orders or {}).get(cid) or {}
    if not isinstance(slot, dict):
        return ""
    order = slot.get(leg)
    if not isinstance(order, dict):
        return ""
    return str(order.get("order_id") or "")


def sister_miss_events(
    *,
    intents: Optional[dict],
    open_orders: Optional[dict],
    now_s: float,
    first_flat_at: Optional[dict] = None,
    last_emit_at: Optional[dict] = None,
    cancel_ttm_s: float = 20.0,
    miss_after_s: float = 10.0,
    throttle_s: float = 30.0,
    filled_shares: Optional[dict] = None,
) -> tuple[list[dict], dict, dict]:
    """Miss rows when A sold leg L and B has no bid, throttled.

    Returns ``(events, next_first_flat_at, next_last_emit_at)``. An event
    fires once ``age_s`` from the sold/first-observed clock is at least
    ``miss_after_s`` and the previous emit for that leg is older than
    ``throttle_s``. Window must still be open past ``cancel_ttm_s``.
    """
    now = float(now_s)
    wait = max(0.0, float(miss_after_s or 0))
    throttle = max(0.0, float(throttle_s or 0))
    cancel_at = float(cancel_ttm_s or 0)
    remembered = first_flat_at or {}
    previous_emit = last_emit_at or {}
    next_flat: dict[str, float] = {}
    next_emit: dict[str, float] = {}
    for key, raw in previous_emit.items():
        ts = _finite_ts(raw)
        if ts is not None and now + 1e-12 < ts + throttle:
            next_emit[str(key)] = ts
    events: list[dict] = []
    for cid, intent in (intents or {}).items():
        if not isinstance(intent, dict):
            continue
        end_ts = _finite_ts(intent.get("end_ts"))
        if end_ts is None:
            continue
        ttm = float(end_ts) - now
        if ttm <= cancel_at + 1e-12:
            continue
        for leg in ("up", "dn"):
            _flat, why = a_token_flat(intent, leg)
            if why != "a_flat":
                continue
            if _leg_filled(filled_shares, str(cid), leg) > 0:
                continue
            key = f"{cid}:{leg}"
            since = flat_since_s(
                intent, now_s=now, remembered=remembered.get(key)
            )
            next_flat[key] = since
            if _order_id(open_orders, str(cid), leg):
                continue
            age = now - since
            if age + 1e-12 < wait:
                continue
            prev = next_emit.get(key)
            if prev is not None and now + 1e-12 < prev + throttle:
                continue
            events.append(
                {
                    "event": "scrapbid_miss",
                    "condition_id": str(cid),
                    "leg": leg,
                    "slug": intent.get("slug"),
                    "ttm": round(ttm, 3),
                    "age_s": round(age, 3),
                }
            )
            next_emit[key] = now
    return events, next_flat, next_emit


def sister_poll_s(
    *,
    intents: Optional[dict],
    open_orders: Optional[dict],
    now_s: float,
    poll_s: float = 2.0,
    hot_poll_s: float = 1.0,
    cancel_ttm_s: float = 20.0,
    enabled: bool = True,
    filled_shares: Optional[dict] = None,
    shares: float = 20.0,
) -> float:
    """Idle poll, or the hot cadence while a sold leg still needs a B bid."""
    idle = max(0.2, float(poll_s or 0))
    if not enabled:
        return idle
    hot = max(0.2, float(hot_poll_s or idle))
    now = float(now_s)
    cancel_at = float(cancel_ttm_s or 0)
    for cid, intent in (intents or {}).items():
        if not isinstance(intent, dict):
            continue
        end_ts = _finite_ts(intent.get("end_ts"))
        if end_ts is None:
            continue
        ttm = float(end_ts) - now
        if ttm <= cancel_at + 1e-12:
            continue
        target = float(shares or 0)
        for leg in ("up", "dn"):
            _flat, why = a_token_flat(intent, leg)
            if why != "a_flat":
                continue
            if target > 0 and _leg_filled(filled_shares, str(cid), leg) + 1e-9 >= target:
                continue
            if _order_id(open_orders, str(cid), leg):
                continue
            return min(idle, hot)
    return idle
