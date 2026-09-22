"""Sister scrap-bidder policy (wallet B). No CLOB posts, no mintbot import.

Wallet A (mintbot) mints and sells. Wallet B only rests small bids. Once A
has ``sold_loser`` on leg L, B bids L immediately, including while A's scrap
rest is still up and outside the last ``active_ttm_s``. Markets A never held
can take a bid on the cheap live side during that late window. The winner
leg A still holds stays blocked. B never mints and never sells.

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
    "bid_max_px": 0.04,
    "active_ttm_s": 180.0,
    "cancel_ttm_s": 20.0,
    "min_gtd_ahead_s": 60.0,
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
    A is long (``a_rest_live``). A missing intent is ``a_absent``.
    """
    if leg not in ("up", "dn"):
        return False, "bad_leg"
    if not isinstance(intent, dict):
        return True, "a_absent"
    status = str(intent.get("status") or "")
    if status not in _HELD_STATUSES:
        return True, "a_absent"
    sold_leg = _sold_leg(intent)
    if sold_leg == leg:
        return True, "a_flat"
    if sold_leg in ("up", "dn") and sold_leg != leg:
        return False, "a_holds_other"
    rest_id = intent.get("sell_scrap_rest_id")
    rest_leg = intent.get("sell_loser_leg") if intent.get("sell_loser_leg") in ("up", "dn") else None
    if rest_id and (rest_leg is None or rest_leg == leg):
        return False, "a_rest_live"
    return False, "a_still_long"


def sister_cancel_due(
    *, ttm_s: Optional[float], cancel_ttm_s: float, window_open: bool
) -> tuple[bool, str]:
    """Cancel a resting sister bid at T−cancel or when the window is over."""
    if not window_open or ttm_s is None or float(ttm_s) <= 0:
        return True, "window_end"
    if float(ttm_s) <= float(cancel_ttm_s) + 1e-12:
        return True, "cancel_before_expiry"
    return False, "keep"


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
    bid_max_px: float = 0.04,
    active_ttm_s: float = 180.0,
    cancel_ttm_s: float = 20.0,
    min_gtd_ahead_s: float = 60.0,
    enabled: bool = True,
) -> list[dict]:
    """Resting-bid plan for one pass. Does not post.

    Each row is ``place``, ``cancel``, ``keep``, or ``skip``.
    """
    intents = intents or {}
    open_orders = open_orders or {}
    actions: list[dict] = []
    size = float(shares or 0)
    px = round(float(bid_max_px or 0), 4)
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
            if size <= 0 or px <= 0 or not tokens.get(leg):
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
            tif, exp = resting_tif(
                now_s=now_s,
                expire_ts=float(end_ts) - float(cancel_ttm_s),
                min_ahead_s=min_gtd_ahead_s,
            )
            actions.append(
                {
                    **base,
                    "op": "place",
                    "reason": flat_why,
                    "price": px,
                    "shares": size,
                    "tif": tif,
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
) -> float:
    """Idle poll, or the hot cadence while a sold leg has no B bid."""
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
        for leg in ("up", "dn"):
            _flat, why = a_token_flat(intent, leg)
            if why != "a_flat":
                continue
            if _order_id(open_orders, str(cid), leg):
                continue
            return min(idle, hot)
    return idle
