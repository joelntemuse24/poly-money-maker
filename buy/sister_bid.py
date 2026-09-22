"""Sister scrap-bidder policy (wallet B). No CLOB posts, no mintbot import.

Wallet A (mintbot) mints and sells. Wallet B only rests small bids, and only
on a token A is already flat on when A held that market. Markets A never
held can take a bid on the cheap live side during the last few minutes.
B never mints and never sells.

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


def a_token_flat(intent: Any, leg: str) -> tuple[bool, str]:
    """Whether wallet A is flat on ``leg`` (or never held this market).

    When A has a live bag, B may bid ``leg`` only after ``sold_loser`` on
    that leg and the scrap rest is gone. The other leg is the winner A
    still holds. A missing intent is ``a_absent`` (B may bid the cheap book).
    """
    if leg not in ("up", "dn"):
        return False, "bad_leg"
    if not isinstance(intent, dict):
        return True, "a_absent"
    status = str(intent.get("status") or "")
    if status not in _HELD_STATUSES:
        return True, "a_absent"
    sold_leg = intent.get("sold_leg") if intent.get("sold_leg") in ("up", "dn") else None
    sold = bool(intent.get("sold_loser") or sold_leg)
    rest_id = intent.get("sell_scrap_rest_id")
    rest_leg = intent.get("sell_loser_leg") if intent.get("sell_loser_leg") in ("up", "dn") else sold_leg
    if rest_id and (rest_leg is None or rest_leg == leg):
        return False, "a_rest_live"
    if sold and sold_leg == leg and not rest_id:
        return True, "a_flat"
    if sold and sold_leg and sold_leg != leg:
        return False, "a_holds_other"
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
            if ttm is None or float(ttm) > float(active_ttm_s) + 1e-12:
                actions.append({**base, "op": "skip", "reason": "too_early"})
                continue
            if cancel:
                actions.append({**base, "op": "skip", "reason": cancel_why})
                continue
            if not flat:
                actions.append({**base, "op": "skip", "reason": flat_why})
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
