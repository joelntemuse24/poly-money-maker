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


def market_rule(cfg: dict, key: str) -> dict:
    """Per-market ``Z``, ``Pmax`` and ``edge_min``. Missing keys fall back."""
    rules = (cfg or {}).get("market_rules") or {}
    row = rules.get(key) or {}
    if not isinstance(row, dict):
        row = {}
    fallback = {"btc_15m": (0.0, 0.97, 0.0), "btc_5m": (0.25, 0.90, 0.0)}.get(key, (0.0, 0.97, 0.0))
    return {
        "Z": _num(row.get("Z"), fallback[0]),
        "Pmax": _num(row.get("Pmax"), fallback[1]),
        "edge_min": _num(row.get("edge_min"), fallback[2]),
    }


def _decision_base(view: dict, strategy: str) -> dict:
    now = _num(view.get("now"), 0.0)
    end_ts = _num(view.get("end_ts"), 0.0)
    return {
        "action": "skip",
        "reason": "no_view",
        "ttm_s": end_ts - now,
        "slug": view.get("slug"),
        "asset": view.get("asset"),
        "duration": view.get("duration"),
        "lane": view.get("lane"),
        "market_key": view.get("key") or view.get("market_key"),
        "strategy": strategy,
        "condition_id": view.get("condition_id"),
        "p": None,
        "q": None,
        "z": None,
        "z_side": None,
        "ask": None,
        "edge": None,
        "side": None,
        "limit": None,
        "notional": 0.0,
        "shares": 0.0,
        "strike": view.get("strike"),
        "live": view.get("live"),
        "twap": view.get("twap"),
        "sigma": view.get("sigma"),
        "expected": view.get("expected"),
        "binance_recv_ts": view.get("binance_recv_ts"),
    }


def _side_book(view: dict, side: str, now: float, stale_s: float) -> dict:
    book = _book(view, side)
    asks = book.get("asks") or []
    if asks and isinstance(asks[0], (tuple, list)):
        ask, ask_sz = best_level(asks)
    else:
        ask = finite_float(book.get("ask"))
        ask_sz = _num(book.get("ask_size"), 0.0)
        asks = [(ask, ask_sz)] if ask is not None else []
    return {
        "side": side,
        "ask": ask,
        "ask_size": ask_sz,
        "asks": asks,
        "bids": book.get("bids") or [],
        "stale": is_stale(book.get("recv_ts"), now, stale_s),
        "token_id": view.get("up_token") if side == "up" else view.get("dn_token"),
        "recv_ts": book.get("recv_ts"),
    }


def _bucket(account: dict, strategy: str) -> float:
    """Dollars this strategy has spent.

    A missing bucket is zero when the other strategy's bucket is present,
    so an s2 fill does not count as s1 spend. An old account that only
    has ``spent`` still counts that total as strategy 1.
    """
    key = "spent_s1" if strategy == "s1" else "spent_s2"
    other = "spent_s2" if strategy == "s1" else "spent_s1"
    if account.get(key) is not None:
        return _num(account.get(key), 0.0)
    if account.get(other) is not None:
        return 0.0
    if strategy == "s1":
        return _num(account.get("spent"), 0.0)
    return 0.0


def _clip_room(account: dict, cfg: dict, *, strategy: str) -> tuple[float, str]:
    """Dollars this clip may spend, and a reason when the room is under the minimum."""
    clip = _num(cfg.get("clip_usd"), 5.0)
    combined = _num(cfg.get("combined_per_market_usd"), _num(cfg.get("per_market_usd"), 40.0))
    if strategy == "s1":
        cap = _num(cfg.get("strategy1_market_usd"), 20.0)
        spent_strategy = _bucket(account, "s1")
    else:
        cap = _num(cfg.get("strategy2_market_usd"), 20.0)
        spent_strategy = _bucket(account, "s2")
    spent_total = _num(account.get("spent_total"), _num(account.get("spent_s1"), 0.0) + _num(account.get("spent_s2"), 0.0))
    exposure = _num(account.get("open_cost"), 0.0)
    max_exposure = _num(cfg.get("max_open_exposure_usd"), 60.0)
    cash = _num(account.get("cash"), 0.0)
    buffer = _num(cfg.get("min_cash_buffer_usd"), 5.0)
    room_strategy = cap - spent_strategy
    room_combined = combined - spent_total
    room_exposure = max_exposure - exposure
    room_cash = cash - buffer
    room = min(clip, room_strategy, room_combined, room_exposure, room_cash)
    min_usd = _num(cfg.get("min_order_usd"), 1.0)
    if room + 1e-9 >= min_usd:
        return room, ""
    if room_strategy <= room_combined and room_strategy <= room_exposure and room_strategy <= room_cash:
        return max(0.0, room), "strategy_cap"
    if room_combined <= room_exposure and room_combined <= room_cash:
        return max(0.0, room), "combined_cap"
    if room_exposure <= room_cash:
        return max(0.0, room), "exposure"
    return max(0.0, room), "cash"


def _finish_buy(base: dict, account: dict, cfg: dict, *, strategy: str, limit: float, now: float, cooldown_s: float, last_ts: Any) -> dict:
    if not bool(cfg.get("enabled", True)):
        base["reason"] = "disabled"
        return base
    if account.get("loss_stopped"):
        base["reason"] = "daily_loss"
        return base
    if last_ts is not None and now - float(last_ts) < float(cooldown_s):
        base["reason"] = "cooldown"
        return base
    room, reason = _clip_room(account, cfg, strategy=strategy)
    if reason:
        base["reason"] = reason
        base["notional"] = room
        return base
    shares = room / float(limit)
    min_shares = _num(cfg.get("min_shares"), 1.0)
    if shares + 1e-9 < min_shares:
        base["reason"] = "min_shares"
        base["notional"] = room
        base["shares"] = shares
        return base
    base["action"] = "buy"
    base["reason"] = "signal"
    base["limit"] = float(limit)
    base["notional"] = room
    base["shares"] = shares
    return base


def _common_skip(view: dict, cfg: dict) -> Optional[str]:
    if not view.get("market_enabled", True):
        return "market_disabled"
    if view.get("resolution_ok") is not True:
        return "resolution_unsupported" if view.get("resolution_source") else "resolution_unknown"
    return None


def evaluate_strategy1(view: dict, account: dict, cfg: dict) -> dict:
    """NIULAI4 ladder. One clip, the Chainlink favourite only.

    After the first fill the side is locked: the other side is never bought
    in this market. Risk blocks still record the z and the ask they refused.
    """
    from buy.lock_fair import side_z

    cfg = cfg or {}
    account = account or {}
    view = view or {}
    base = _decision_base(view, "s1")
    now = _num(view.get("now"), 0.0)
    ttm = base["ttm_s"]

    def skip(reason: str, **extra: Any) -> dict:
        base["reason"] = reason
        base.update(extra)
        return base

    if not bool(cfg.get("strategy1_enabled", True)):
        return skip("strategy_off")
    common = _common_skip(view, cfg)
    if common:
        return skip(common)
    if ttm > _num(cfg.get("s1_tau_max"), 58.0):
        return skip("too_early")
    if ttm < _num(cfg.get("s1_tau_min"), 1.0):
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
    expected = finite_float(view.get("expected"))
    strike = finite_float(view.get("strike"))
    sigma = finite_float(view.get("sigma"))
    if expected is None or strike is None or sigma is None:
        return skip("model_unavailable")
    scored = side_z(
        strike=strike,
        expected=expected,
        sigma=sigma,
        tau_s=max(ttm, 0.0),
        noise_frac=_num(cfg.get("noise_frac"), 0.00002),
    )
    side = scored["side"]
    book = _side_book(view, side, now, _num(cfg.get("stale_book_s"), 2.0))
    rule = market_rule(cfg, str(view.get("key") or view.get("market_key") or ""))
    ask = book["ask"]
    q = scored["q"]
    edge = None if ask is None else (q - float(ask) - taker_fee(float(ask), _num(cfg.get("taker_fee_rate"), 0.07), _num(cfg.get("taker_fee_exponent"), 1.0)))
    base.update(
        {
            "side": side,
            "p": q,
            "q": q,
            "z": scored["z"],
            "z_side": scored["z_side"],
            "sd": scored["sd"],
            "ask": ask,
            "edge": edge,
            "token_id": book["token_id"],
            "asks": book["asks"],
            "bids": book["bids"],
            "book_recv_ts": book["recv_ts"],
            "Z": rule["Z"],
            "Pmax": rule["Pmax"],
        }
    )
    locked = account.get("locked_side")
    if locked and str(locked) != side:
        return skip("side_locked")
    if scored["z_side"] + 1e-12 < rule["Z"]:
        return skip("z_below")
    if book["stale"]:
        return skip("stale_book")
    if ask is None:
        return skip("no_ask")
    ask_f = float(ask)
    ask_min = _num(cfg.get("ask_min"), 0.02)
    max_pay = _num(cfg.get("max_pay"), 0.97)
    if ask_f + 1e-12 < ask_min:
        return skip("ask_below")
    if ask_f - 1e-12 > max_pay:
        return skip("max_pay")
    if ask_f - 1e-12 > rule["Pmax"]:
        return skip("ask_above")
    if edge is None or edge + 1e-12 < rule["edge_min"]:
        return skip("edge_below")
    limit = limit_price(ask_f, cfg)
    if limit is None or limit - 1e-12 > max_pay:
        return skip("max_pay")
    return _finish_buy(
        base,
        account,
        cfg,
        strategy="s1",
        limit=limit,
        now=now,
        cooldown_s=_num(cfg.get("s1_clip_cooldown_s"), 1.0),
        last_ts=account.get("last_s1_ts"),
    )


def evaluate_strategy2(view: dict, account: dict, cfg: dict) -> dict:
    """R2e Binance-move sniper. The side is the move, not the Chainlink favourite.

    ``s2_q_edge_min`` null disables the optional ``q - ask`` filter. This
    path does not lock a side: the wallets buy both sides of one window.
    """
    cfg = cfg or {}
    account = account or {}
    view = view or {}
    base = _decision_base(view, "s2")
    now = _num(view.get("now"), 0.0)
    ttm = base["ttm_s"]

    def skip(reason: str, **extra: Any) -> dict:
        base["reason"] = reason
        base.update(extra)
        return base

    if not bool(cfg.get("strategy2_enabled", True)):
        return skip("strategy_off")
    allowed = cfg.get("strategy2_markets") or ["btc_5m"]
    key = str(view.get("key") or view.get("market_key") or "")
    if key not in {str(item) for item in allowed}:
        return skip("strategy_market_off")
    common = _common_skip(view, cfg)
    if common:
        return skip(common)
    if ttm > _num(cfg.get("s2_tau_max"), 300.0):
        return skip("too_early")
    if ttm < _num(cfg.get("s2_tau_min"), 5.0):
        return skip("too_late")
    if is_stale(view.get("binance_recv_ts"), now, _num(cfg.get("stale_binance_s"), 2.0)):
        return skip("stale_binance")
    move = finite_float(view.get("binance_move"))
    if move is None:
        return skip("move_unavailable")
    base["move"] = move
    base["sigma1s"] = view.get("sigma1s")
    base["sigma_source"] = view.get("sigma_source")
    threshold = _num(cfg.get("s2_move_sigma"), 2.0)
    if abs(move) + 1e-12 < threshold:
        return skip("move_below")
    side = "up" if move > 0 else "down"
    book = _side_book(view, side, now, _num(cfg.get("stale_book_s"), 2.0))
    q_up = finite_float(view.get("q_up"))
    q = None if q_up is None else (q_up if side == "up" else 1.0 - q_up)
    ask = book["ask"]
    base.update(
        {
            "side": side,
            "p": q,
            "q": q,
            "ask": ask,
            "token_id": book["token_id"],
            "asks": book["asks"],
            "bids": book["bids"],
            "book_recv_ts": book["recv_ts"],
        }
    )
    if book["stale"]:
        return skip("stale_book")
    if ask is None:
        return skip("no_ask")
    ask_f = float(ask)
    ask_min = _num(cfg.get("ask_min"), 0.02)
    ask_max = _num(cfg.get("s2_ask_max"), 0.98)
    max_pay = _num(cfg.get("max_pay"), 0.97)
    if ask_f + 1e-12 < ask_min:
        return skip("ask_below")
    if ask_f - 1e-12 > max_pay:
        return skip("max_pay")
    if ask_f - 1e-12 > ask_max:
        return skip("ask_above")
    q_edge = cfg.get("s2_q_edge_min")
    if q_edge is not None and q_edge != "":
        if q is None:
            return skip("model_unavailable")
        edge = float(q) - ask_f
        base["edge"] = edge
        if edge + 1e-12 < float(q_edge):
            return skip("q_edge_below")
    limit = limit_price(ask_f, cfg)
    if limit is None or limit - 1e-12 > max_pay:
        return skip("max_pay")
    return _finish_buy(
        base,
        account,
        cfg,
        strategy="s2",
        limit=limit,
        now=now,
        cooldown_s=_num(cfg.get("s2_clip_cooldown_s"), 1.0),
        last_ts=account.get("last_s2_ts"),
    )


def evaluate_entry(view: dict, account: dict, cfg: dict) -> dict:
    """Strategy 1. Kept so older callers still hit the NIULAI4 rule."""
    return evaluate_strategy1(view, account, cfg)


def note_fill(
    account: dict,
    *,
    cost: float,
    shares: float,
    count_entry: bool = True,
    strategy: str = "s1",
    side: Optional[str] = None,
    now: Optional[float] = None,
) -> dict:
    """Add a fill toward the strategy cap, the combined cap, and open exposure.

    A strategy-1 fill with shares locks ``locked_side``. Later clips on the
    other side are refused. Strategy 2 does not lock.
    """
    account = dict(account or {})
    paid = max(0.0, float(cost))
    account["open_cost"] = _num(account.get("open_cost"), 0.0) + paid
    if strategy == "s2":
        account["spent_s2"] = _num(account.get("spent_s2"), 0.0) + paid
        if now is not None:
            account["last_s2_ts"] = float(now)
    else:
        account["spent_s1"] = _num(account.get("spent_s1"), 0.0) + paid
        if now is not None:
            account["last_s1_ts"] = float(now)
        if side and float(shares) > 0:
            account["locked_side"] = str(side)
    account["spent_total"] = _num(account.get("spent_s1"), 0.0) + _num(account.get("spent_s2"), 0.0)
    account["spent"] = account["spent_total"]
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
