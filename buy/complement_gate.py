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


def funder_from_env_file(path: object) -> str:
    """Read FUNDER_ADDRESS from a dotenv file. Ignores process env.

    systemd EnvironmentFile= injects complement vars before Python starts.
    load_dotenv('.env') will not override those, so os.getenv would compare
    the complement funder to itself and refuse to start.
    """
    raw = str(path or "")
    if not raw:
        return ""
    try:
        with open(raw, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != "FUNDER_ADDRESS":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()
    return ""


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


def _amount_to_shares(raw: object, expected: float) -> Optional[float]:
    """Human shares or 1e6 fixed-point. None if unusable."""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0 or value in (float("inf"), float("-inf")):
        return None
    expected_f = _finite(expected, minimum=0) or 0.0
    text = str(raw).strip().lower()
    if value >= 10_000 and "." not in text and "e" not in text:
        fixed = value / 1_000_000.0
        if expected_f > 0 and abs(fixed - expected_f) <= abs(value - expected_f):
            return fixed
        if expected_f <= 0:
            return fixed
    return value


def complement_fill_from_post(result: Any, requested: float) -> Tuple[str, float]:
    """Classify a CLOB POST body. Never treat a missing body as a full fill.

    Returns ``(status, shares)``: filled / empty / ambiguous.
    """
    if result is None:
        return "ambiguous", 0.0
    data: Dict[str, Any]
    if isinstance(result, dict):
        data = result
    else:
        data = {}
        for key in (
            "status", "success", "errorMsg", "error", "size_matched",
            "takingAmount", "taking_amount", "orderID", "order_id",
        ):
            if hasattr(result, key):
                data[key] = getattr(result, key)
    err = str(data.get("errorMsg") or data.get("error") or "").lower()
    if "no orders found to match" in err:
        return "empty", 0.0
    status = str(data.get("status") or "").strip().lower()
    want = _finite(requested, minimum=0) or 0.0
    matched = _amount_to_shares(data.get("size_matched"), want)
    taking = _amount_to_shares(
        data.get("takingAmount", data.get("taking_amount")), want,
    )
    shares = 0.0
    if matched is not None and matched > 0:
        shares = float(matched)
    elif taking is not None and taking > 0:
        shares = float(taking)
    if status in {"delayed"}:
        return "ambiguous", 0.0
    # size_matched is fill evidence. takingAmount without a terminal
    # matched status can be the signed order size on a delayed POST.
    if matched is not None and matched > 0 and status in {"", "matched", "order_status_matched"}:
        return "filled", float(matched)
    if taking is not None and taking > 0 and status in {"matched", "order_status_matched"}:
        return "filled", float(taking)
    if status in {"matched", "order_status_matched"} and shares <= 0:
        return "empty", 0.0
    if data.get("success") is False:
        return "rejected", 0.0
    return "ambiguous", 0.0


def apply_balance_evidence(
    status: str,
    shares: float,
    *,
    baseline: Optional[float],
    after: Optional[float],
) -> Tuple[str, float]:
    """Unmatched 400 + inventory bump is a ghost fill; unread balance stays ambiguous."""
    base = _finite(baseline, minimum=0)
    later = _finite(after, minimum=0)
    if later is not None and base is not None:
        delta = later - base
        if delta > 0.01:
            return "filled", float(delta)
        if status == "empty":
            return "empty", 0.0
        if status == "filled" and shares > 0:
            return "filled", float(shares)
        return status, shares
    if status == "empty" and (base is None or later is None):
        return "ambiguous", 0.0
    return status, shares


def mark_submit_quarantine(
    meta: Dict[str, Any],
    *,
    token: str,
    shares: float,
    limit: float,
    baseline: Optional[float],
    now_s: float,
    leg: str = "",
    source: str = "",
    slug: str = "",
    held_token: str = "",
) -> Dict[str, Any]:
    """Write-ahead marker. Must hit disk before the CLOB POST."""
    row = dict(meta or {})
    row["buy_uncertain"] = True
    row["buy_uncertain_at"] = float(now_s)
    row["buy_uncertain_token"] = str(token)
    row["buy_uncertain_shares"] = float(shares)
    row["buy_uncertain_price"] = float(limit)
    if leg:
        row["buy_uncertain_leg"] = str(leg)
    if source:
        row["source"] = str(source)
    if slug:
        row["slug"] = str(slug)
    if held_token:
        row["primary_held"] = str(held_token)
    if baseline is None:
        row.pop("buy_uncertain_baseline", None)
    else:
        row["buy_uncertain_baseline"] = float(baseline)
    return row


def _clear_uncertain(meta: Dict[str, Any]) -> None:
    for key in (
        "buy_uncertain",
        "buy_uncertain_at",
        "buy_uncertain_token",
        "buy_uncertain_shares",
        "buy_uncertain_price",
        "buy_uncertain_baseline",
        "buy_uncertain_order_id",
        "buy_uncertain_leg",
    ):
        meta.pop(key, None)


def apply_complement_outcome(
    meta: Dict[str, Any],
    *,
    status: str,
    shares: float,
    token: str,
    leg: str,
    source: str,
    slug: str,
    held_token: str,
    now_s: float,
    empty_cooldown_s: float,
    reject_cooldown_s: float,
) -> Dict[str, Any]:
    """Persist fill, empty/reject cooldown, or keep in-flight quarantine."""
    if status == "filled" and shares > 0.01:
        _clear_uncertain(meta)
        meta.pop("cooldown_until", None)
        meta["bought_token"] = str(token)
        meta["bought_leg"] = str(leg)
        meta["bought_size"] = float(shares)
        meta["source"] = str(source)
        meta["primary_held"] = str(held_token)
        meta["slug"] = str(slug)
        meta["filled_at"] = float(now_s)
        return meta
    if status == "ambiguous":
        meta["buy_uncertain"] = True
        return meta
    _clear_uncertain(meta)
    cool = float(reject_cooldown_s if status == "rejected" else empty_cooldown_s)
    meta["cooldown_until"] = float(now_s) + max(0.0, cool)
    return meta


def resolve_inflight(
    meta: Any,
    *,
    now_s: float,
    after: Optional[float],
    timeout_s: float,
) -> Tuple[str, float]:
    """Classify leftover write-ahead quarantine. Never invent a fill.

    Returns ``(status, shares)``: filled / empty / wait.
    Flat readable balances become empty only after ``timeout_s`` so a
    delayed match can still show up. Unreadable balances stay ``wait``
    (fail closed — do not POST again).
    """
    if not isinstance(meta, dict) or not meta.get("buy_uncertain"):
        return "wait", 0.0
    baseline = meta.get("buy_uncertain_baseline")
    status, shares = apply_balance_evidence(
        "ambiguous", 0.0, baseline=baseline, after=after,
    )
    if status == "filled" and shares > 0.01:
        return "filled", shares
    base = _finite(baseline, minimum=0)
    later = _finite(after, minimum=0)
    started = _finite(meta.get("buy_uncertain_at"), minimum=0) or 0.0
    aged = float(now_s) - float(started)
    if later is not None and base is not None and (later - base) <= 0.01:
        if aged + 1e-12 >= float(timeout_s):
            return "empty", 0.0
        return "wait", 0.0
    return "wait", 0.0


def should_block_post(meta: Any, now_s: float) -> Tuple[bool, str]:
    """Block a new FAK while filled, in-flight, or cooling down."""
    if not isinstance(meta, dict) or not meta:
        return False, ""
    size = _finite(meta.get("bought_size"), minimum=0) or 0.0
    if meta.get("bought_token") and size > 0.01:
        return True, "already_bought"
    if meta.get("buy_uncertain"):
        return True, "in_flight"
    until = _finite(meta.get("cooldown_until"), minimum=0) or 0.0
    if until > float(now_s):
        return True, "cooldown"
    return False, ""
