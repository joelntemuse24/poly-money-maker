"""Second-account complement buyer — arm + fire rules (no I/O).

Primary 5m/15m hedge logic is untouched. This module only reads a snapshot
of the first account's positions JSON and decides whether the isolated
complement wallet should lift the *other* token at ≥80¢.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class ArmedMarket:
    condition_id: str
    source: str
    held_token: str
    held_leg: str
    held_shares: float
    other_token: str
    other_leg: str
    end_ts: float
    slug: str
    start_ts: float = 0.0


def _norm_addr(value: object) -> str:
    return str(value or "").strip().lower()


def primary_and_complement_same_wallet(primary: object, complement: object) -> bool:
    """Fail closed: empty or matching funders are the same wallet."""
    a = _norm_addr(primary)
    b = _norm_addr(complement)
    if not a or not b:
        return True
    return a == b


def other_leg_token(
    bought_leg: object,
    up_token: object,
    dn_token: object,
    *,
    bought_token: object = "",
) -> Tuple[str, str]:
    """Return (other_token, other_leg) for a confirmed primary fill."""
    up = str(up_token or "")
    dn = str(dn_token or "")
    held = str(bought_token or "")
    leg = str(bought_leg or "").strip().lower()
    if leg in {"down", "dn"}:
        return up, "up"
    if leg == "up":
        return dn, "down"
    if held and held == up:
        return dn, "down"
    if held and held == dn:
        return up, "up"
    return "", ""


def _finite(value: object, *, minimum: Optional[float] = None) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    if minimum is not None and parsed < minimum:
        return None
    return parsed


def arm_from_primary_meta(
    blob: Any,
    *,
    source: str,
    now_s: float,
    grace_s: float = 45.0,
    min_size: float = 0.01,
) -> List[ArmedMarket]:
    """Arm complement watch from one first-account positions JSON object."""
    if not isinstance(blob, dict):
        return []
    out: List[ArmedMarket] = []
    grace = float(grace_s)
    for raw_cid, meta in blob.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("hedge_closed") or meta.get("redeem_pending") or meta.get("redeem_confirmed"):
            continue
        if meta.get("buy_uncertain") and not meta.get("bought_token"):
            continue
        token = str(meta.get("bought_token") or "")
        size = _finite(meta.get("bought_size", 0), minimum=0) or 0.0
        if not token or size <= float(min_size):
            continue
        up = str(meta.get("up_token") or "")
        dn = str(meta.get("dn_token") or "")
        other, other_leg = other_leg_token(
            meta.get("bought_leg"), up, dn, bought_token=token,
        )
        if not other or other == token:
            continue
        end_ts = _finite(meta.get("end_ts", 0), minimum=0) or 0.0
        if end_ts > 0 and float(now_s) > end_ts + grace:
            continue
        held_leg = str(meta.get("bought_leg") or "").strip().lower()
        if held_leg in {"down", "dn"}:
            held_leg = "down"
        elif held_leg != "up":
            held_leg = "up" if token == up else "down"
        out.append(
            ArmedMarket(
                condition_id=str(raw_cid),
                source=str(source),
                held_token=token,
                held_leg=held_leg,
                held_shares=float(size),
                other_token=other,
                other_leg=other_leg,
                end_ts=float(end_ts),
                slug=str(meta.get("slug") or raw_cid),
                start_ts=_finite(meta.get("start_ts", 0), minimum=0) or 0.0,
            )
        )
    return out


def complement_target_shares(
    held_shares: float,
    *,
    ask: float,
    limit: float,
    spend_cap: float,
    share_cap: float = 0.0,
) -> float:
    """Share-match the primary bag, clipped to spend/share caps. 2 dp shares."""
    held = _finite(held_shares, minimum=0) or 0.0
    lim = _finite(limit, minimum=0) or 0.0
    ask_f = _finite(ask, minimum=0) or 0.0
    cap = _finite(spend_cap, minimum=0) or 0.0
    if held < 0.01 or lim <= 0 or ask_f <= 0 or cap < 0.01:
        return 0.0
    share_tick = Decimal("0.01")
    cent = Decimal("0.01")
    want = Decimal(str(held)).quantize(share_tick, rounding=ROUND_DOWN)
    cap_d = Decimal(str(cap)).quantize(cent, rounding=ROUND_DOWN)
    lim_d = Decimal(str(lim))
    max_sh = (cap_d / lim_d).quantize(share_tick, rounding=ROUND_DOWN)
    max_cap = _finite(share_cap, minimum=0)
    if max_cap is not None and max_cap > 0:
        max_sh = min(
            max_sh,
            Decimal(str(max_cap)).quantize(share_tick, rounding=ROUND_DOWN),
        )
    shares = min(want, max_sh)
    if shares < share_tick:
        return 0.0
    while shares >= share_tick:
        maker = shares * lim_d
        if maker.quantize(cent) == maker:
            return float(shares)
        shares -= share_tick
        shares = shares.quantize(share_tick, rounding=ROUND_DOWN)
    return 0.0


def evaluate_complement(
    *,
    other_ask: Optional[float],
    other_bid: Optional[float],
    held_shares: float,
    already_bought: bool,
    primary_still_holding: bool,
    oracle_favors_other: Optional[bool] = True,
    min_price: float = 0.80,
    max_price: float = 0.99,
    max_spread: float = 0.05,
    spend_cap: float = 16.0,
    share_cap: float = 20.0,
    require_oracle: bool = True,
) -> Tuple[bool, str, float]:
    """Whether the complement wallet should FAK the other token.

    Returns ``(fire, reason, shares)``. Shares are 0 when not firing.
    """
    if already_bought:
        return False, "already_bought", 0.0
    if not primary_still_holding:
        return False, "primary_flat", 0.0
    ask = _finite(other_ask, minimum=0)
    if ask is None:
        return False, "no_ask", 0.0
    if ask + 1e-12 < float(min_price):
        return False, "ask_below_min", 0.0
    if ask > float(max_price) + 1e-12:
        return False, "ask_above_max", 0.0
    bid = _finite(other_bid, minimum=0)
    if bid is None or bid <= 0:
        return False, "no_bid", 0.0
    if (ask - bid) > float(max_spread) + 1e-12:
        return False, "wide_book", 0.0
    if require_oracle and not oracle_favors_other:
        return False, "oracle_still_held", 0.0
    shares = complement_target_shares(
        held_shares,
        ask=ask,
        limit=float(max_price),
        spend_cap=float(spend_cap),
        share_cap=float(share_cap),
    )
    if shares < 0.01:
        return False, "size_zero", 0.0
    return True, "fire", shares


def merge_armed(batches: Iterable[Iterable[ArmedMarket]]) -> List[ArmedMarket]:
    """Dedupe by condition_id (first source wins)."""
    seen: Dict[str, ArmedMarket] = {}
    for batch in batches:
        for row in batch:
            seen.setdefault(row.condition_id, row)
    return list(seen.values())
