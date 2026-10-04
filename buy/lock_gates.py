"""Entry gates, caps, staleness, and the Dublin-day loss stop.

Pure: no network, no clock reads. The caller passes ``now`` and the
in-memory book and oracle view.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from buy.book import finite_float
from buy.lock_fair import taker_fee


DUBLIN = ZoneInfo("Europe/Dublin")


def dublin_day(ts: float) -> str:
    """Calendar day in Europe/Dublin, ``YYYY-MM-DD``."""
    return datetime.fromtimestamp(float(ts), tz=DUBLIN).date().isoformat()


def age_s(recv_ts: Any, now: float) -> Optional[float]:
    recv = finite_float(recv_ts)
    if recv is None:
        return None
    return float(now) - recv


def is_stale(recv_ts: Any, now: float, limit_s: float) -> bool:
    """True when the local receive time is missing or older than ``limit_s``."""
    age = age_s(recv_ts, now)
    return age is None or age > float(limit_s)


def round_to_tick(price: float, tick: float, *, mode: str = "nearest") -> float:
    step = float(tick)
    if step <= 0:
        return float(price)
    units = float(price) / step
    if mode == "down":
        n = math.floor(units + 1e-9)
    elif mode == "up":
        n = math.ceil(units - 1e-9)
    else:
        n = round(units)
    return round(n * step, 6)


def parse_levels(levels: Any, side: str) -> list[tuple[float, float]]:
    """Valid book levels. Asks are cheapest first, bids richest first."""
    merged: dict[float, float] = {}
    if not levels:
        return []
    for level in levels:
        if isinstance(level, dict):
            price = finite_float(level.get("price"))
            size = finite_float(level.get("size"))
        elif isinstance(level, (tuple, list)) and len(level) >= 2:
            price = finite_float(level[0])
            size = finite_float(level[1])
        else:
            continue
        if price is None or size is None or not 0 < price < 1 or size <= 0:
            continue
        key = round(price, 4)
        merged[key] = merged.get(key, 0.0) + size
    rows = list(merged.items())
    rows.sort(key=lambda item: item[0], reverse=(side == "bid"))
    return rows


def best_level(levels: Sequence[tuple[float, float]]) -> tuple[Optional[float], float]:
    if not levels:
        return None, 0.0
    price, size = levels[0]
    return float(price), float(size)


def taker_edge(p_win: float, ask: float, cfg: dict) -> float:
    """``p - ask - taker_fee(ask)``, the backtest edge."""
    fee = taker_fee(
        ask,
        _num(cfg.get("taker_fee_rate"), 0.07),
        _num(cfg.get("taker_fee_exponent"), 1.0),
    )
    return float(p_win) - float(ask) - fee


def limit_price(ask: float, cfg: dict) -> Optional[float]:
    """FAK limit at the ask, plus ``limit_tick_improve`` ticks, never above ``max_pay``."""
    tick = _num(cfg.get("price_tick"), 0.01)
    improve = int(_num(cfg.get("limit_tick_improve"), 0))
    cap = _num(cfg.get("max_pay"), 0.97)
    raw = float(ask) + max(0, improve) * tick
    raw = min(raw, cap)
    limit = round_to_tick(raw, tick, mode="down")
    if limit + 1e-9 < float(ask) or limit <= 0 or limit >= 1:
        return None
    return limit


def simulate_fak_buy(
    asks: Sequence[tuple[float, float]],
    limit: float,
    notional: float,
) -> dict:
    """Walk asks at or under ``limit`` until the dollar cap is spent.

    A dry-run fill. No order is posted.
    """
    room = max(0.0, float(notional))
    shares = 0.0
    cost = 0.0
    for price, size in asks:
        if price - 1e-12 > float(limit):
            break
        if room <= 1e-9 or size <= 0:
            break
        take = min(float(size), room / float(price))
        shares += take
        spent = take * float(price)
        cost += spent
        room -= spent
    vwap = (cost / shares) if shares > 0 else None
    return {"shares": shares, "cost": cost, "vwap": vwap, "posted": False}


def execute_buy(plan: dict, *, dry_run: bool, poster: Any = None) -> dict:
    """Dry-run walks the book. Live mode is the only path that calls ``poster``."""
    if dry_run:
        fill = simulate_fak_buy(plan.get("asks") or [], float(plan["limit"]), float(plan["notional"]))
        fill["dry_run"] = True
        return fill
    if poster is None:
        raise RuntimeError("live buy requires a poster")
    result = poster(plan)
    if not isinstance(result, dict):
        result = {"raw": result}
    result["dry_run"] = False
    result["posted"] = True
    return result


def buy_spent_usd(result: Any, offered_usd: float) -> float:
    """USDC paid on a BUY. ``makingAmount`` is the collateral leg."""
    if not isinstance(result, dict):
        return 0.0
    offered = float(offered_usd or 0)
    for key in ("makingAmount", "making_amount", "usd_matched", "cost"):
        raw = result.get(key)
        if raw is None or raw == "":
            continue
        try:
            human = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(human) or human <= 0:
            continue
        fixed = human / 1_000_000.0
        if offered > 0:
            return human if abs(human - offered) <= abs(fixed - offered) else fixed
        return fixed if human >= 10_000 else human
    return 0.0


def day_pnl(positions: Sequence[dict], now: float, marks: Optional[dict] = None) -> float:
    """Realized P&L settled on this Dublin day, plus mark-to-bid of opens.

    A position with no bid and no stored mark is left out, so a missing
    book does not look like a total loss. Marks are bid prices of the
    held token.
    """
    today = dublin_day(now)
    marks = marks or {}
    total = 0.0
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        settled = pos.get("settled_ts")
        if settled:
            if dublin_day(float(settled)) != today:
                continue
            total += _num(pos.get("pnl"), 0.0)
            continue
        token = str(pos.get("token_id") or "")
        mark = finite_float(marks.get(token))
        if mark is None:
            mark = finite_float(pos.get("last_mark"))
        if mark is None:
            continue
        total += _num(pos.get("shares"), 0.0) * mark - _num(pos.get("cost"), 0.0)
    return total


def loss_stop_active(pnl: float, stop_usd: float, latched_day: Optional[str], today: str) -> bool:
    """Latched for the rest of the Europe/Dublin day once loss passes the stop."""
    if latched_day and latched_day == today:
        return True
    return float(pnl) <= -abs(float(stop_usd))


def open_exposure_usd(positions: Sequence[dict]) -> float:
    total = 0.0
    for pos in positions or []:
        if not isinstance(pos, dict) or pos.get("settled_ts"):
            continue
        total += max(0.0, _num(pos.get("cost"), 0.0))
    return total


def _num(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _book(view: dict, side: str) -> dict:
    book = view.get(side) or {}
    return book if isinstance(book, dict) else {}


def evaluate_entry(view: dict, account: dict, cfg: dict) -> dict:
    """One market, one decision. ``action`` is ``buy`` or ``skip``.

    Risk blocks are applied after a qualifying price, so a disabled bot
    or a daily stop still records the edge it refused.
    """
    cfg = cfg or {}
    account = account or {}
    view = view or {}
    now = _num(view.get("now"), _num(account.get("now"), 0.0))
    end_ts = _num(view.get("end_ts"), 0.0)
    ttm = end_ts - now
    base = {
        "action": "skip",
        "reason": "no_view",
        "ttm_s": ttm,
        "slug": view.get("slug"),
        "asset": view.get("asset"),
        "duration": view.get("duration"),
        "lane": view.get("lane"),
        "strategy": "twap_lock",
        "condition_id": view.get("condition_id"),
        "p": None,
        "ask": None,
        "edge": None,
        "side": None,
        "limit": None,
        "notional": 0.0,
        "shares": 0.0,
    }

    def skip(reason: str, **extra: Any) -> dict:
        base["reason"] = reason
        base.update(extra)
        return base

    if not view.get("market_enabled", True):
        return skip("market_disabled")
    if view.get("resolution_ok") is not True:
        return skip("resolution_unsupported" if view.get("resolution_source") else "resolution_unknown")

    entry_window = _num(cfg.get("entry_window_s"), 60.0)
    entry_min = _num(cfg.get("entry_min_ttm_s"), 1.0)
    if ttm > entry_window:
        return skip("too_early")
    if ttm < entry_min:
        return skip("too_late")

    if is_stale(view.get("live_recv_ts"), now, _num(cfg.get("stale_price_s"), 2.0)):
        return skip("stale_live")
    if is_stale(view.get("twap_recv_ts"), now, _num(cfg.get("stale_price_s"), 2.0)):
        return skip("stale_twap")

    strike_reason = str(view.get("strike_reason") or "")
    if strike_reason in {"strike_unknown", "strike_mismatch"} or view.get("strike") is None:
        return skip(strike_reason or "strike_unknown")
    if not view.get("coverage_ok", False):
        return skip("elapsed_gap")
    vol_n = int(_num(view.get("vol_samples"), 0))
    if vol_n < int(_num(cfg.get("vol_min_samples"), 60)):
        return skip("vol_short", vol_samples=vol_n)

    p_up = finite_float(view.get("p_up"))
    if p_up is None:
        return skip("model_unavailable")

    p_min = _num(cfg.get("p_min"), 0.80)
    ask_min = _num(cfg.get("ask_min"), 0.30)
    ask_max = _num(cfg.get("ask_max"), 0.95)
    max_pay = _num(cfg.get("max_pay"), 0.97)
    edge_min = _num(cfg.get("edge_min"), 0.03)
    stale_book = _num(cfg.get("stale_book_s"), 2.0)

    candidates = []
    for side, p_win in (("up", p_up), ("down", 1.0 - p_up)):
        book = _book(view, side)
        asks = book.get("asks") or []
        if asks and isinstance(asks[0], (tuple, list)):
            ask, ask_sz = best_level(asks)
        else:
            ask = finite_float(book.get("ask"))
            ask_sz = _num(book.get("ask_size"), 0.0)
            asks = [(ask, ask_sz)] if ask is not None else []
        edge = None if ask is None else taker_edge(p_win, ask, cfg)
        candidates.append(
            {
                "side": side,
                "p": p_win,
                "ask": ask,
                "ask_size": ask_sz,
                "asks": asks,
                "bids": book.get("bids") or [],
                "edge": edge,
                "stale": is_stale(book.get("recv_ts"), now, stale_book),
                "token_id": view.get("up_token") if side == "up" else view.get("dn_token"),
            }
        )
    candidates.sort(key=lambda row: (row["p"], row["edge"] if row["edge"] is not None else -9), reverse=True)

    def consider(side_row: dict) -> tuple[str, Optional[float]]:
        if side_row["p"] + 1e-12 < p_min:
            return "p_below", None
        if side_row["stale"] or side_row["ask"] is None:
            return ("stale_book" if side_row["stale"] else "no_ask"), None
        ask = float(side_row["ask"])
        if ask - 1e-12 > max_pay:
            return "max_pay", None
        if ask - 1e-12 > ask_max:
            return "ask_above", None
        if ask + 1e-12 < ask_min:
            return "ask_below", None
        if side_row["edge"] is None or side_row["edge"] + 1e-12 < edge_min:
            return "edge_below", None
        limit = limit_price(ask, cfg)
        if limit is None:
            return "max_pay", None
        if limit > ask + 1e-9 and taker_edge(side_row["p"], limit, cfg) + 1e-12 < edge_min:
            return "edge_below", limit
        return "edge", limit

    chosen = None
    chosen_limit = None
    fallback_reason = "p_below"
    for side_row in candidates:
        reason, limit = consider(side_row)
        if reason == "edge":
            chosen = side_row
            chosen_limit = limit
            break
        if side_row is candidates[0]:
            fallback_reason = reason
            chosen = side_row
            chosen_limit = limit
    best = chosen or candidates[0]
    base.update(
        {
            "side": best["side"],
            "p": best["p"],
            "ask": best["ask"],
            "edge": best["edge"],
            "token_id": best["token_id"],
            "asks": best["asks"],
            "bids": best["bids"],
            "strike": view.get("strike"),
            "gamma_strike": view.get("gamma_strike"),
            "live": view.get("live"),
            "twap": view.get("twap"),
            "sigma": view.get("sigma"),
            "expected": view.get("expected"),
        }
    )
    if chosen_limit is not None and chosen is not None and consider(chosen)[0] == "edge":
        base["limit"] = chosen_limit
    else:
        return skip(fallback_reason)

    if not bool(cfg.get("enabled", True)):
        return skip("disabled")
    if account.get("loss_stopped"):
        return skip("daily_loss")

    entries = int(_num(account.get("entries"), 0))
    max_entries = int(_num(cfg.get("max_entries_per_market"), 1))
    if entries >= max_entries:
        return skip("max_entries")

    per_market = _num(cfg.get("per_market_usd"), 20.0)
    spent = _num(account.get("spent"), 0.0)
    exposure = _num(account.get("open_cost"), 0.0)
    max_exposure = _num(cfg.get("max_open_exposure_usd"), 100.0)
    cash = _num(account.get("cash"), 0.0)
    buffer = _num(cfg.get("min_cash_buffer_usd"), 5.0)
    room_market = per_market - spent
    room_exposure = max_exposure - exposure
    room_cash = cash - buffer
    room = min(room_market, room_exposure, room_cash, per_market)
    min_usd = _num(cfg.get("min_order_usd"), 1.0)
    if room + 1e-9 < min_usd:
        if room_market <= room_exposure and room_market <= room_cash:
            return skip("per_market_cap", notional=max(0.0, room))
        if room_exposure <= room_cash:
            return skip("exposure", notional=max(0.0, room))
        return skip("cash", notional=max(0.0, room))

    shares = room / limit
    min_shares = _num(cfg.get("min_shares"), 5.0)
    if shares + 1e-9 < min_shares:
        return skip("min_shares", notional=room, shares=shares)

    base["action"] = "buy"
    base["reason"] = "edge"
    base["notional"] = room
    base["shares"] = shares
    return base


def note_fill(account: dict, *, cost: float, shares: float, count_entry: bool = True) -> dict:
    """Add a fill toward the per-market cap and the open exposure."""
    account = dict(account or {})
    account["spent"] = _num(account.get("spent"), 0.0) + max(0.0, float(cost))
    account["open_cost"] = _num(account.get("open_cost"), 0.0) + max(0.0, float(cost))
    if count_entry:
        account["entries"] = int(_num(account.get("entries"), 0)) + 1
    account["last_shares"] = float(shares)
    return account


def utc_now_iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def settle_pnl(
    *,
    side: str,
    shares: float,
    cost: float,
    fee: float,
    final_twap: float,
    strike: float,
) -> dict:
    """Hold-to-settlement P&L. Up wins ties (final TWAP >= strike)."""
    up_wins = float(final_twap) + 1e-12 >= float(strike)
    won = up_wins if str(side) == "up" else (not up_wins)
    payout = float(shares) * (1.0 if won else 0.0)
    pnl = payout - float(cost) - float(fee)
    return {"won": won, "up_wins": up_wins, "payout": payout, "pnl": pnl}
