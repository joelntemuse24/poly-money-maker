"""Paper log of an early high-price loser scrap. No orders.

Live sells ignore this module. It latches each bag with the same
``scrap_mode_for_open`` rule, then, only in ``early`` mode, applies the
live loser persist clocks. A paper fill walks the bids already in hand
down to ``shadow_early_min_px`` and labels every derived number ``est_``.
That walk is not a queue position and it is not a latency model.

State stays in memory. One ``shadow_bag_result`` line is appended to
``logs/shadow_early_scrap.jsonl`` when the sell window ends. A later
redeem numerator can add ``shadow_resolution``. Nothing here writes the
mint intent.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from buy.mint_sell import (
    classify_loser,
    loser_scrap_persist_s,
    lower_priced_side,
    persist_ready,
    recorded_fill_px,
    scrap_min_px,
    scrap_mode_for_open,
    scrap_mode_latch_due,
    scrap_share_plan,
)

EST_NOTE = (
    "estimate from the displayed bid ladder at or above shadow_early_min_px; "
    "no queue position and no latency"
)
ASSUMPTION = (
    "est_scrap_delta_usd is shadow scrap proceeds minus live scrap proceeds only. "
    "Dump, kept, and reclaim cash are assumed identical to live except "
    "est_dump_share_delta, the shares an early shadow scrap would have removed "
    "from a later live sell of that same leg. est_ fields walk the displayed "
    "book and ignore queue position and latency."
)

_LOCK = threading.Lock()
_BAGS: dict[str, dict] = {}
_LOG_PATH: Optional[Path] = None
_LOGGER = None


def set_log_path(path: Any) -> None:
    """Where ``shadow_bag_result`` lines are appended. Tests set a temp file."""
    global _LOG_PATH
    _LOG_PATH = None if path is None else Path(path)


def set_logger(logger) -> None:
    """Optional ``log_event`` used by the resolution note. Tick events return."""
    global _LOGGER
    _LOGGER = logger


def reset_for_tests() -> None:
    """Drop in-memory bags. Does not remove a log file."""
    with _LOCK:
        _BAGS.clear()


def shadow_enabled(cfg: Optional[dict]) -> bool:
    return bool((cfg or {}).get("shadow_early_scrap_enabled", False))


def _knobs(cfg: dict) -> tuple[float, float, float, float]:
    threshold = scrap_min_px(cfg.get("shadow_early_threshold", 0.20)) or 0.20
    floor = scrap_min_px(cfg.get("shadow_early_min_px", 0.15)) or 0.15
    opposite = scrap_min_px(cfg.get("shadow_early_opposite_min", 0.75)) or 0.75
    raw = cfg.get("shadow_early_fraction", 0.5)
    if raw is None or raw == "":
        raw = 0.5
    try:
        fraction = float(raw)
    except (TypeError, ValueError):
        fraction = 0.5
    if fraction != fraction or fraction <= 0 or fraction > 1:
        fraction = 0.5
    return threshold, floor, opposite, fraction


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def estimate_bid_walk(
    levels: Any,
    want: float,
    min_px: float,
) -> dict:
    """Sell ``want`` shares into displayed bids, stopping under ``min_px``.

    Higher bids are taken first. Prices below ``min_px`` are not used.
    ``est_filled_size`` can be shorter than ``want`` when the ladder runs out.
    """
    merged: dict[float, float] = {}
    for level in levels or []:
        if not isinstance(level, dict):
            continue
        price = _num(level.get("price"))
        size = _num(level.get("size"))
        if price is None or size is None or size <= 0 or not 0 < price < 1:
            continue
        key = round(price, 4)
        merged[key] = merged.get(key, 0.0) + size
    ladder = sorted(merged.items(), key=lambda item: item[0], reverse=True)
    best_bid_size = 0.0
    if ladder:
        best_bid_size = float(ladder[0][1])
    need = max(0.0, float(want or 0))
    filled = 0.0
    cash = 0.0
    floor = float(min_px)
    for price, size in ladder:
        if price + 1e-12 < floor:
            break
        if filled + 1e-12 >= need:
            break
        take = min(float(size), need - filled)
        if take <= 0:
            break
        filled += take
        cash += take * float(price)
    vwap = (cash / filled) if filled > 1e-12 else None
    return {
        "best_bid_size": round(best_bid_size, 4),
        "est_filled_size": round(filled, 4),
        "est_vwap": None if vwap is None else round(vwap, 4),
        "est_proceeds": round(cash, 4),
        "est_note": EST_NOTE,
    }


def winning_side(num_up: Any, num_dn: Any) -> Optional[str]:
    """Payout numerators: the strictly larger side, or None when they tie."""
    up = _num(num_up)
    dn = _num(num_dn)
    if up is None or dn is None:
        return None
    if up > dn:
        return "up"
    if dn > up:
        return "dn"
    return None


def dump_share_delta(
    *,
    shadow_leg: Optional[str],
    shadow_size: float,
    live_leg: Optional[str],
    live_size: float,
    dump_leg: Optional[str],
    dump_size: float,
    dump_px: Optional[float],
    kept_leg: Optional[str],
    kept_size: float,
    kept_px: Optional[float],
) -> tuple[float, Optional[float], float]:
    """Shares a shadow scrap would remove from a later live sell of that leg.

    Returns ``(est_dump_share_delta, est_dump_px, est_dump_cash_delta)``.
    When the shadow leg is the live scrap leg, only the surplus over the
    live scrap can collide with a later dump or kept sell. A shadow sale of
    the other leg collides with whatever live later sold of that leg.
    """
    if shadow_leg not in ("up", "dn") or shadow_size <= 0:
        return 0.0, None, 0.0
    already = live_size if live_leg == shadow_leg else 0.0
    surplus = max(0.0, float(shadow_size) - float(already))
    taken = 0.0
    cash = 0.0
    weighted = 0.0

    def _take(leg: Optional[str], size: float, px: Optional[float]) -> None:
        nonlocal taken, cash, weighted
        if leg != shadow_leg or size <= 0 or taken + 1e-12 >= surplus:
            return
        use = min(float(size), surplus - taken)
        if use <= 0:
            return
        taken += use
        if px is not None:
            cash += use * float(px)
            weighted += use

    _take(dump_leg, dump_size, dump_px)
    if kept_leg != dump_leg:
        _take(kept_leg, kept_size, kept_px)
    px = (cash / weighted) if weighted > 1e-12 else None
    return (
        round(taken, 4),
        None if px is None else round(px, 4),
        round(cash, 4),
    )


def _append(event: str, fields: dict, now_s: float) -> None:
    path = _LOG_PATH
    if path is None:
        return
    row = {"ts": now_s, "event": event, **fields}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=str, sort_keys=True) + "\n")
    except Exception:
        return


def _bag(condition_id: str) -> dict:
    row = _BAGS.get(condition_id)
    if row is None:
        row = {
            "mode": None,
            "armed_ts": None,
            "filled": False,
            "floor_logged": False,
            "result_logged": False,
            "resolution_logged": False,
            "winning_side": None,
            "fill": None,
        }
        _BAGS[condition_id] = row
    return row


def observe(
    cfg: Optional[dict],
    *,
    condition_id: str,
    slug: Any,
    now_s: float,
    end_ts: float,
    ttm_s: Optional[float],
    shares: float,
    up_bid: Optional[float],
    dn_bid: Optional[float],
    up_bids: Any,
    dn_bids: Any,
    up_bid_size: Any = None,
    dn_bid_size: Any = None,
) -> list[tuple[str, dict]]:
    """One paper tick. Returns log events. Does not place an order."""
    if not shadow_enabled(cfg):
        return []
    cfg = cfg or {}
    events: list[tuple[str, dict]] = []
    threshold, floor, opposite, fraction = _knobs(cfg)
    with _LOCK:
        rec = _bag(str(condition_id))
        if rec["mode"] not in ("old", "early") and scrap_mode_latch_due(
            ttm_s, cfg.get("sell_scrap_max_ttm_s"), rec["mode"],
        ):
            mode = scrap_mode_for_open(up_bid, dn_bid, floor)
            rec["mode"] = mode
            side = lower_priced_side(up_bid, dn_bid)
            events.append((
                "shadow_scrap_mode",
                {
                    "condition_id": condition_id,
                    "slug": slug,
                    "mode": mode,
                    "leg": None if side is None else side[0],
                    "loser_bid": None if side is None else side[1],
                    "other_bid": None if side is None else side[2],
                    "ttm": ttm_s,
                    "floor": floor,
                },
            ))
        if rec["mode"] != "early" or rec["filled"]:
            return events
        loser, _reason = classify_loser(
            up_bid, dn_bid, threshold=threshold, opposite_min=opposite,
        )
        if loser not in ("up", "dn"):
            rec["armed_ts"] = None
            return events
        bid = up_bid if loser == "up" else dn_bid
        levels = up_bids if loser == "up" else dn_bids
        quoted_size = up_bid_size if loser == "up" else dn_bid_size
        live_bid = _num(bid)
        if live_bid is None or live_bid + 1e-12 < floor:
            rec["armed_ts"] = None
            if not rec["floor_logged"]:
                rec["floor_logged"] = True
                events.append((
                    "shadow_scrap_floor_skip",
                    {
                        "condition_id": condition_id,
                        "slug": slug,
                        "leg": loser,
                        "bid": live_bid,
                        "floor": floor,
                        "ttm": ttm_s,
                    },
                ))
            return events
        persist_s, _why = loser_scrap_persist_s(
            now_s=now_s,
            end_ts=end_ts,
            persist_s=float(cfg.get("sell_persist_s") or 0.0),
            last_min_s=float(cfg.get("sell_persist_last_min_s", 2.0) or 0.0),
            last_min_window_s=float(
                cfg.get("sell_persist_last_min_window_s", 60.0) or 0.0
            ),
            skip_when_sized=False,
        )
        if persist_s is None:
            return events
        fire, armed, _how = persist_ready(
            True,
            now_s=now_s,
            armed_ts=rec["armed_ts"],
            persist_s=persist_s,
        )
        rec["armed_ts"] = armed
        if not fire:
            return events
        planned, _keep = scrap_share_plan(shares, fraction)
        walked = estimate_bid_walk(levels, planned, floor)
        if _num(quoted_size) is not None:
            walked["best_bid_size"] = round(float(quoted_size), 4)
        rec["filled"] = True
        rec["fill"] = {
            "leg": loser,
            "bid": live_bid,
            "size": planned,
            "ttm": ttm_s,
            **walked,
        }
        events.append((
            "shadow_scrap_fill",
            {
                "condition_id": condition_id,
                "slug": slug,
                "leg": loser,
                "ttm": ttm_s,
                "bid": live_bid,
                "best_bid_size": walked["best_bid_size"],
                "size": planned,
                "est_filled_size": walked["est_filled_size"],
                "est_vwap": walked["est_vwap"],
                "est_proceeds": walked["est_proceeds"],
                "est_note": EST_NOTE,
            },
        ))
    return events


def _live_facts(intent: Optional[dict]) -> dict:
    intent = intent or {}
    sold = bool(intent.get("sold_loser") or intent.get("sold_leg"))
    live_leg = intent.get("sold_leg") if sold else None
    if live_leg not in ("up", "dn"):
        live_leg = None
    live_size = _num(intent.get("sell_filled")) if live_leg else None
    live_px = (
        _num(recorded_fill_px(intent, "sell_fill_px", "sell_limit"))
        if live_leg else None
    )
    dump_fired = bool(intent.get("sold_dump"))
    dump_leg = intent.get("sell_dump_leg") if dump_fired else None
    if dump_leg not in ("up", "dn"):
        dump_leg = None
        dump_fired = False if not intent.get("sold_dump") else dump_fired
    dump_size = _num(intent.get("sell_dump_filled")) if dump_leg else None
    dump_px = (
        _num(recorded_fill_px(intent, "sell_dump_fill_px", "sell_dump_limit"))
        if dump_leg else None
    )
    kept_leg = intent.get("sell_dump_kept_leg")
    if kept_leg not in ("up", "dn"):
        kept_leg = None
    kept_size = _num(intent.get("sell_dump_kept_filled")) if kept_leg else None
    kept_px = (
        _num(recorded_fill_px(intent, "sell_dump_kept_fill_px", "sell_dump_kept_limit"))
        if kept_leg else None
    )
    return {
        "live_scrap_leg": live_leg,
        "live_scrap_size": live_size,
        "live_scrap_avg_px": live_px,
        "live_dump_fired": bool(intent.get("sold_dump")),
        "live_dump_leg": dump_leg,
        "live_dump_size": dump_size,
        "live_dump_avg_px": dump_px,
        "live_dump_kept_leg": kept_leg,
        "live_dump_kept_size": kept_size,
        "live_dump_kept_avg_px": kept_px,
    }


def build_bag_result(rec: dict, intent: Optional[dict], *, condition_id: str, slug: Any) -> dict:
    """One honest paper-versus-live summary. Estimated keys start with ``est_``."""
    fill = rec.get("fill") or {}
    live = _live_facts(intent)
    shadow_size = float(fill.get("size") or 0.0) if rec.get("filled") else 0.0
    shadow_proceeds = _num(fill.get("est_proceeds")) if rec.get("filled") else 0.0
    if shadow_proceeds is None:
        shadow_proceeds = 0.0
    live_proceeds = 0.0
    if live["live_scrap_size"] is not None and live["live_scrap_avg_px"] is not None:
        live_proceeds = float(live["live_scrap_size"]) * float(live["live_scrap_avg_px"])
    share_delta, dump_px, cash_delta = dump_share_delta(
        shadow_leg=fill.get("leg") if rec.get("filled") else None,
        shadow_size=shadow_size,
        live_leg=live["live_scrap_leg"],
        live_size=float(live["live_scrap_size"] or 0.0),
        dump_leg=live["live_dump_leg"],
        dump_size=float(live["live_dump_size"] or 0.0),
        dump_px=live["live_dump_avg_px"],
        kept_leg=live["live_dump_kept_leg"],
        kept_size=float(live["live_dump_kept_size"] or 0.0),
        kept_px=live["live_dump_kept_avg_px"],
    )
    winner = rec.get("winning_side")
    return {
        "condition_id": condition_id,
        "slug": slug,
        "mode": rec.get("mode"),
        "shadow_leg": fill.get("leg") if rec.get("filled") else None,
        "shadow_bid": fill.get("bid") if rec.get("filled") else None,
        "shadow_size": fill.get("size") if rec.get("filled") else None,
        "est_vwap": fill.get("est_vwap") if rec.get("filled") else None,
        "est_proceeds": fill.get("est_proceeds") if rec.get("filled") else None,
        "est_filled_size": fill.get("est_filled_size") if rec.get("filled") else None,
        "est_note": EST_NOTE,
        **live,
        "winning_side": winner if winner in ("up", "dn") else None,
        "winning_side_reason": (
            "payout_numerators" if winner in ("up", "dn") else "pending_resolution"
        ),
        "est_scrap_delta_usd": round(float(shadow_proceeds) - live_proceeds, 4),
        "est_dump_share_delta": share_delta,
        "est_dump_px": dump_px,
        "est_dump_cash_delta": cash_delta,
        "assumption": ASSUMPTION,
    }


def close_bag(
    cfg: Optional[dict],
    *,
    condition_id: str,
    slug: Any,
    intent: Optional[dict],
    now_s: float,
) -> list[tuple[str, dict]]:
    """Log the paper result once when the sell window ends."""
    if not shadow_enabled(cfg):
        return []
    with _LOCK:
        rec = _BAGS.get(str(condition_id))
        if rec is None or rec.get("result_logged"):
            return []
        fields = build_bag_result(
            rec, intent, condition_id=condition_id, slug=slug,
        )
        rec["result_logged"] = True
        _append("shadow_bag_result", fields, now_s)
    return [("shadow_bag_result", fields)]


def note_resolution(
    condition_id: str,
    *,
    num_up: Any,
    num_dn: Any,
    slug: Any = None,
    now_s: float = 0.0,
) -> list[tuple[str, dict]]:
    """Remember which side paid. One follow-up line if the result is already out."""
    side = winning_side(num_up, num_dn)
    events: list[tuple[str, dict]] = []
    with _LOCK:
        rec = _BAGS.get(str(condition_id))
        if rec is None or side is None:
            return []
        rec["winning_side"] = side
        if not rec.get("result_logged") or rec.get("resolution_logged"):
            return []
        rec["resolution_logged"] = True
        fields = {
            "condition_id": condition_id,
            "slug": slug if slug is not None else None,
            "winning_side": side,
            "winning_side_reason": "resolution",
            "num_up": num_up,
            "num_dn": num_dn,
        }
        _append("shadow_resolution", fields, now_s)
        events.append(("shadow_resolution", fields))
    logger = _LOGGER
    if logger is not None:
        try:
            for event, fields in events:
                logger(event, **fields)
        except Exception:
            return events
    return events
