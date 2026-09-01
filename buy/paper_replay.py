"""Paper replay of live 5m / 15m entry + hedge gates on 1s books.

No orders. Does not import buybot*. Used by check_92c_week_backtest.py
and unit tests. Pathlog TOB is preferred; last-trade reconstruction is
the public-API fallback when ticks are not on disk.

Not replayed: Chainlink/PTB oracle, empty FAK 400s, POST RTT.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from buy.hedge_gate import (
    hedge_ladder_for_ttm,
    hedge_qualify_ok,
    held_hedge_decision,
)

GUI_SPREAD = 0.10
ASK_92_MIN = 0.92
ASK_92_MAX = 0.929999999999  # 92.0–92.9¢ on a 0.001 book; 0.92 on 0.01
BUDGET = 2.5
BUY_MAX_SPEND = 3.0
BUY_MAX_SHARES = 5.0
MAX_ENTRY_SPREAD = 0.05
MIN_WINNER_BID = 0.70
MAX_LOSER_BID = 0.30
MIN_BID_EDGE = 0.05

# Live 5m B+C + last-30s ladder (strategy_buy5m.example.json / code defaults).
FIVE_DUMP = 0.40
FIVE_QUALIFY = 0.50
FIVE_ASK_MAX = 0.52
FIVE_RECOVERY = 0.53
FIVE_PERSIST_S = 1.0
FIVE_SPREAD = 0.15
FIVE_FLATTEN_MAX = 0.75
FIVE_TOXIC_BELOW = 0.75
FIVE_LATE_TTM = 30.0
FIVE_LATE_DUMP = 0.40
FIVE_LATE_QUALIFY = 0.58
FIVE_LATE_ASK_MAX = 0.60
FIVE_LATE_RECOVERY = 0.62

# Stopped 15m: instant 35/40 + inverted 70/30 GUI (buybot.py).
FIFTEEN_THRESHOLD = 0.35
FIFTEEN_ASK_MAX = 0.40
FIFTEEN_SPREAD = 0.15
FIFTEEN_TOXIC_BELOW = 0.65


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        px = float(value)
    except (TypeError, ValueError):
        return None
    if px != px:
        return None
    return px


def display_price(bid: Any, ask: Any, last_trade: Any = None) -> Optional[float]:
    """Polymarket GUI: mid if spread ≤ 10¢, else last trade."""
    bid_f = _f(bid)
    ask_f = _f(ask)
    last_f = _f(last_trade)
    if bid_f is not None and ask_f is not None:
        if ask_f < bid_f:
            return None
        if (ask_f - bid_f) <= GUI_SPREAD + 1e-12:
            return (bid_f + ask_f) / 2.0
    return last_f


def entry_book_ok(bid, ask, max_spread=MAX_ENTRY_SPREAD, min_bid=MIN_WINNER_BID):
    bid_f, ask_f = _f(bid), _f(ask)
    spread_max, floor = _f(max_spread), _f(min_bid)
    if bid_f is None or ask_f is None or spread_max is None or floor is None:
        return False, "missing_side"
    if ask_f < bid_f:
        return False, "crossed"
    if (ask_f - bid_f) > spread_max + 1e-12:
        return False, "wide_spread"
    if bid_f + 1e-12 < floor:
        return False, "bid_too_low"
    return True, "ok"


def hedge_consensus_ok(
    held_bid, held_ask, held_last,
    other_bid, other_ask, other_last,
    *,
    held_gui_max,
    other_gui_min,
    min_edge=MIN_BID_EDGE,
    last_trade_max,
) -> Tuple[bool, str]:
    """Same gates as live 5m ``hedge_consensus_ok`` (copied, no bot import)."""
    held_last_f = _f(held_last)
    last_max = _f(last_trade_max)
    held_cap = _f(held_gui_max)
    other_floor = _f(other_gui_min)
    edge = _f(min_edge)
    if held_last_f is None or last_max is None:
        return False, "missing_last_trade"
    if held_last_f > last_max + 1e-12:
        return False, "last_trade_too_high"
    held_gui = display_price(held_bid, held_ask, held_last_f)
    other_gui = display_price(other_bid, other_ask, other_last)
    if held_gui is None or other_gui is None:
        return False, "incomplete_gui"
    if held_cap is None or other_floor is None or edge is None:
        return False, "missing_gui_limits"
    if abs(held_gui - other_gui) + 1e-12 < edge:
        return False, "ambiguous"
    if held_gui > held_cap + 1e-12:
        return False, "held_gui_too_high"
    if other_gui + 1e-12 < other_floor:
        return False, "other_gui_too_low"
    return True, "ok"


def quoted_buy_shares(budget: float, ask: float, share_cap: Optional[float] = None) -> float:
    """15m sizer: ``budget/ask`` at 2 dp shares, exact-cent maker."""
    if budget < 0.01 or ask <= 0:
        return 0.0
    spend = Decimal(str(budget)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    ask_d = Decimal(str(ask))
    if spend < Decimal("0.01") or ask_d <= 0:
        return 0.0
    share_tick = Decimal("0.01")
    min_shares = Decimal("0.01")
    cent = Decimal("0.01")
    shares = (spend / ask_d).quantize(share_tick, rounding=ROUND_DOWN)
    if share_cap is not None:
        shares = min(
            shares,
            Decimal(str(share_cap)).quantize(share_tick, rounding=ROUND_DOWN),
        )
    while shares >= min_shares:
        maker = shares * ask_d
        if maker.quantize(cent) == maker:
            return float(shares)
        shares -= share_tick
        shares = shares.quantize(share_tick, rounding=ROUND_DOWN)
    return 0.0


def quoted_buy_shares_up_to_limit(
    budget: float,
    ask: float,
    limit: float,
    share_cap: Optional[float] = None,
    spend_cap: Optional[float] = None,
) -> float:
    """5m FAK at band max: at least 3 shares when ``3 × limit`` fits spend cap."""
    shares = quoted_buy_shares(budget, ask, share_cap)
    if limit <= 0:
        return 0.0
    ceiling = spend_cap if spend_cap is not None and spend_cap >= 0.01 else budget
    if ceiling < 0.01:
        return 0.0
    share_tick = Decimal("0.01")
    min_shares = Decimal("0.01")
    cent = Decimal("0.01")
    lim = Decimal(str(limit))
    cap_d = Decimal(str(ceiling)).quantize(cent, rounding=ROUND_DOWN)
    max_sh = (cap_d / lim).quantize(share_tick, rounding=ROUND_DOWN)
    if share_cap is not None:
        max_sh = min(
            max_sh,
            Decimal(str(share_cap)).quantize(share_tick, rounding=ROUND_DOWN),
        )
    target = Decimal(str(shares if shares >= 0.01 else 0))
    three = Decimal("3.00")
    if three <= max_sh and three * lim <= cap_d:
        target = max(target, three)
    if target < min_shares:
        return 0.0

    def _valid(sh: Decimal) -> bool:
        if sh < min_shares or sh > max_sh:
            return False
        maker = sh * lim
        return maker.quantize(cent) == maker and maker <= cap_d

    up = target.quantize(share_tick, rounding=ROUND_DOWN)
    if up < target:
        up += share_tick
        up = up.quantize(share_tick, rounding=ROUND_DOWN)
    while up <= max_sh:
        if _valid(up):
            return float(up)
        up += share_tick
        up = up.quantize(share_tick, rounding=ROUND_DOWN)
    down = min(target, max_sh).quantize(share_tick, rounding=ROUND_DOWN)
    while down >= min_shares:
        if _valid(down):
            return float(down)
        down -= share_tick
        down = down.quantize(share_tick, rounding=ROUND_DOWN)
    return 0.0


def fak_fill(series: str, ask: float, ask_size: Optional[float] = None) -> dict:
    """Size the FAK. 5m posts at 92¢; 15m pins the limit to the quoted ask."""
    if series == "5m":
        shares = quoted_buy_shares_up_to_limit(
            BUDGET, ask, ASK_92_MIN, BUY_MAX_SHARES, BUY_MAX_SPEND,
        )
        limit = ASK_92_MIN
    else:
        shares = quoted_buy_shares(BUDGET, ask, BUY_MAX_SHARES)
        limit = ask
    out = {
        "status": "zero",
        "shares": 0.0,
        "notional": 0.0,
        "avg": None,
        "limit": limit,
        "ask": ask,
    }
    if shares < 0.01 or limit <= 0:
        return out
    fill_sh = shares
    if ask_size is not None:
        fill_sh = min(shares, math.floor(float(ask_size) * 10000 + 1e-12) / 10000)
    if fill_sh < 0.01:
        return out
    avg = limit if series == "5m" else ask
    # Optimistic: fill at the posted limit (5m) / quoted ask (15m). No walk.
    notional = fill_sh * avg
    status = "full"
    if ask_size is not None and ask_size + 1e-12 < shares:
        status = "partial"
    out.update(status=status, shares=fill_sh, notional=notional, avg=avg)
    return out


def _leg_quote(row: dict, leg: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if leg == "up":
        return _f(row.get("ub")), _f(row.get("ua")), _f(row.get("ult") or row.get("ua"))
    return _f(row.get("db")), _f(row.get("da")), _f(row.get("dlt") or row.get("da"))


def ticks_from_trades(
    trades: Sequence[dict],
    start_ts: float,
    end_ts: float,
) -> List[dict]:
    """One tick per unix second. Last print per side, complement bid.

    ``ub ≈ min(ua, 1-da)`` so a 92/8 tape is a tight 92 book. Seconds with
    only one side printed are skipped until both legs have a print, then
    forward-filled so persist-1s sees wall clock.
    """
    by_sec: Dict[int, List[dict]] = {}
    for row in trades:
        ts = _f(row.get("ts") if "ts" in row else row.get("timestamp"))
        px = _f(row.get("px") if "px" in row else row.get("price"))
        if ts is None or px is None:
            continue
        sec = int(ts)
        if sec < int(start_ts) or sec > int(end_ts):
            continue
        outcome = str(row.get("outcome") or "").lower()
        size = _f(row.get("size"))
        by_sec.setdefault(sec, []).append(
            {"outcome": outcome, "px": px, "size": size}
        )
    up_px = dn_px = None
    up_sz = dn_sz = None
    ticks: List[dict] = []
    t0 = int(start_ts)
    t1 = int(end_ts)
    for sec in range(t0, t1 + 1):
        for tr in by_sec.get(sec, []):
            if tr["outcome"] == "up":
                up_px, up_sz = tr["px"], tr["size"]
            elif tr["outcome"] == "down":
                dn_px, dn_sz = tr["px"], tr["size"]
        if up_px is None or dn_px is None:
            continue
        ua, da = float(up_px), float(dn_px)
        ub = min(ua, max(0.001, round(1.0 - da, 4)))
        db = min(da, max(0.001, round(1.0 - ua, 4)))
        ticks.append(
            {
                "e": "tick",
                "ts": float(sec),
                "ttm": float(end_ts - sec),
                "ua": ua,
                "da": da,
                "ub": ub,
                "db": db,
                "ult": ua,
                "dlt": da,
                "uas": up_sz,
                "das": dn_sz,
                "ubs": up_sz,
                "dbs": dn_sz,
            }
        )
    return ticks


def ask_is_92(ask: Optional[float]) -> bool:
    if ask is None:
        return False
    return ASK_92_MIN - 1e-12 <= float(ask) <= ASK_92_MAX + 1e-12


def first_92_entry(
    ticks: Sequence[dict],
    *,
    ttm_max: float,
    ttm_min: float = 0.0,
) -> Optional[dict]:
    """First in-window tick where the winning ask is 92¢ and live entry gates pass."""
    for row in ticks:
        ttm = _f(row.get("ttm"))
        if ttm is None or not (ttm_min <= ttm <= ttm_max):
            continue
        ub, ua, ult = _leg_quote(row, "up")
        db, da, dlt = _leg_quote(row, "down")
        up_gui = display_price(ub, ua, ult)
        dn_gui = display_price(db, da, dlt)
        if up_gui is None or dn_gui is None:
            continue
        if abs(up_gui - dn_gui) + 1e-12 < MIN_BID_EDGE:
            continue
        if up_gui > dn_gui:
            leg, ask, bid, last, ask_size = "up", ua, ub, ult, _f(row.get("uas"))
            win_gui, lose_gui = up_gui, dn_gui
        elif dn_gui > up_gui:
            leg, ask, bid, last, ask_size = "down", da, db, dlt, _f(row.get("das"))
            win_gui, lose_gui = dn_gui, up_gui
        else:
            continue
        if not ask_is_92(ask):
            continue
        book_ok, why = entry_book_ok(bid, ask)
        if not book_ok:
            continue
        if win_gui + 1e-12 < MIN_WINNER_BID or lose_gui > MAX_LOSER_BID + 1e-12:
            continue
        return {
            "ts": row.get("ts"),
            "ttm": ttm,
            "leg": leg,
            "ask": ask,
            "bid": bid,
            "last": last,
            "ask_size": ask_size,
            "why": why,
        }
    return None


def _after_entry(row: dict, hit: dict) -> bool:
    hit_ts = _f(hit.get("ts"))
    row_ts = _f(row.get("ts"))
    if hit_ts is not None and row_ts is not None:
        return row_ts > hit_ts + 1e-9
    hit_ttm = _f(hit.get("ttm"))
    row_ttm = _f(row.get("ttm"))
    if hit_ttm is not None and row_ttm is not None:
        return row_ttm < hit_ttm - 1e-9
    return False


def _redeem(held: str, fill: dict, winner: Optional[str]) -> dict:
    shares = float(fill.get("shares") or 0.0)
    notional = float(fill.get("notional") or 0.0)
    if winner is None:
        return {
            "exit": "unresolved",
            "exit_reason": "unresolved",
            "exit_bid": None,
            "exit_ttm": None,
            "won": None,
            "pnl": None,
            "hedge_late": False,
        }
    won = held == winner
    return {
        "exit": "redeem_win" if won else "redeem_loss",
        "exit_reason": "redeem",
        "exit_bid": None,
        "exit_ttm": None,
        "won": won,
        "pnl": round((shares - notional) if won else -notional, 4),
        "hedge_late": False,
    }


def walk_5m_held(
    ticks: Sequence[dict],
    hit: dict,
    fill: dict,
    winner: Optional[str],
) -> dict:
    """Live 5m dump / persist-1s / fade / recovery / last-30s ladder / flatten."""
    shares = float(fill.get("shares") or 0.0)
    notional = float(fill.get("notional") or 0.0)
    avg = _f(fill.get("avg"))
    held = hit.get("leg")
    if shares <= 0 or notional <= 0 or held not in ("up", "down"):
        return {
            "exit": "no_fill",
            "exit_reason": "no_fill",
            "exit_bid": None,
            "exit_ttm": None,
            "won": None,
            "pnl": 0.0 if winner else None,
            "hedge_late": False,
        }
    toxic = avg is not None and avg < FIVE_TOXIC_BELOW - 1e-12
    other = "down" if held == "up" else "up"
    armed = None
    persist_done = False
    for row in ticks:
        if not _after_entry(row, hit):
            continue
        bid, ask, last = _leg_quote(row, held)
        other_bid, other_ask, other_last = _leg_quote(row, other)
        ttm = _f(row.get("ttm"))
        ts = _f(row.get("ts"))
        if ts is None:
            continue
        ladder = hedge_ladder_for_ttm(
            ttm,
            FIVE_DUMP,
            FIVE_QUALIFY,
            FIVE_ASK_MAX,
            FIVE_RECOVERY,
            late_ttm=FIVE_LATE_TTM,
            late_dump=FIVE_LATE_DUMP,
            late_qualify=FIVE_LATE_QUALIFY,
            late_ask_max=FIVE_LATE_ASK_MAX,
            late_recovery=FIVE_LATE_RECOVERY,
        )
        gui_ok, gui_why = hedge_consensus_ok(
            bid, ask, last,
            other_bid, other_ask, other_last,
            held_gui_max=ladder.ask_max,
            other_gui_min=round(1.0 - ladder.ask_max, 4),
            min_edge=MIN_BID_EDGE,
            last_trade_max=ladder.ask_max,
        )
        intent = held_hedge_decision(
            bid, ask, bid, ask, bid, ask,
            now_s=ts,
            persist_armed_ts=armed,
            persist_s=FIVE_PERSIST_S,
            persist_done=persist_done,
            oracle_agrees=False,
            dump_ignore_oracle=True,
            dump_bid_max=FIVE_DUMP,
            qualify_bid=FIVE_QUALIFY,
            qualify_ask_max=FIVE_ASK_MAX,
            recovery_cancel=FIVE_RECOVERY,
            sell_fade=True,
            max_spread=FIVE_SPREAD,
            gui_ok=gui_ok,
            gui_why=gui_why,
            flatten=toxic,
            flatten_max=FIVE_FLATTEN_MAX,
            seconds_left=ttm,
            late_ttm=FIVE_LATE_TTM,
            late_dump=FIVE_LATE_DUMP,
            late_qualify=FIVE_LATE_QUALIFY,
            late_ask_max=FIVE_LATE_ASK_MAX,
            late_recovery=FIVE_LATE_RECOVERY,
        )
        persist_done = bool(intent.persist_done)
        armed = intent.persist_ts
        if intent.action not in {"sell", "dump"}:
            continue
        px = float(intent.sell_at)
        bid_sz = _f(row.get("ubs" if held == "up" else "dbs"))
        sell = shares
        if bid_sz is not None:
            sell = min(shares, bid_sz)
        sell = math.floor(sell * 10000 + 1e-12) / 10000
        if sell <= 0:
            continue
        proceeds = sell * px
        remain = max(0.0, shares - sell)
        label = "dump" if intent.dump else "hedge"
        if intent.reason == "flatten_walk":
            label = "flatten"
        if remain >= 0.01:
            remain_val = remain if winner == held else 0.0
            pnl = round(proceeds + remain_val - notional, 4)
        else:
            pnl = round(proceeds - notional, 4)
        return {
            "exit": label,
            "exit_reason": intent.reason,
            "exit_bid": px,
            "exit_ttm": ttm,
            "won": False,
            "pnl": pnl,
            "hedge_late": bool(ladder.late),
            "winner_dump": bool(winner == held),
        }
    return _redeem(held, fill, winner)


def walk_15m_held(
    ticks: Sequence[dict],
    hit: dict,
    fill: dict,
    winner: Optional[str],
) -> dict:
    """Stopped 15m: toxic dump at ≤35¢; else 35/40 book + inverted 70/30 GUI."""
    shares = float(fill.get("shares") or 0.0)
    notional = float(fill.get("notional") or 0.0)
    avg = _f(fill.get("avg"))
    held = hit.get("leg")
    if shares <= 0 or notional <= 0 or held not in ("up", "down"):
        return {
            "exit": "no_fill",
            "exit_reason": "no_fill",
            "exit_bid": None,
            "exit_ttm": None,
            "won": None,
            "pnl": 0.0 if winner else None,
            "hedge_late": False,
        }
    toxic = avg is not None and avg < FIFTEEN_TOXIC_BELOW - 1e-12
    other = "down" if held == "up" else "up"
    for row in ticks:
        if not _after_entry(row, hit):
            continue
        bid, ask, last = _leg_quote(row, held)
        other_bid, other_ask, other_last = _leg_quote(row, other)
        ttm = _f(row.get("ttm"))
        if bid is None:
            continue
        if toxic and bid <= FIFTEEN_THRESHOLD + 1e-12:
            return {
                "exit": "dump",
                "exit_reason": "toxic_bid",
                "exit_bid": bid,
                "exit_ttm": ttm,
                "won": False,
                "pnl": round(shares * bid - notional, 4),
                "hedge_late": False,
                "winner_dump": bool(winner == held),
            }
        ok, why = hedge_qualify_ok(
            bid, ask, FIFTEEN_THRESHOLD, FIFTEEN_SPREAD, FIFTEEN_ASK_MAX,
        )
        if not ok:
            continue
        gui_ok, gui_why = hedge_consensus_ok(
            bid, ask, last,
            other_bid, other_ask, other_last,
            held_gui_max=MAX_LOSER_BID,
            other_gui_min=MIN_WINNER_BID,
            min_edge=MIN_BID_EDGE,
            last_trade_max=FIFTEEN_ASK_MAX,
        )
        if not gui_ok:
            continue
        return {
            "exit": "hedge",
            "exit_reason": gui_why if gui_why == "ok" else why,
            "exit_bid": bid,
            "exit_ttm": ttm,
            "won": False,
            "pnl": round(shares * bid - notional, 4),
            "hedge_late": False,
            "winner_dump": bool(winner == held),
        }
    return _redeem(held, fill, winner)


def evaluate_market(
    ticks: Sequence[dict],
    *,
    series: str,
    ttm_max: float,
    winner: Optional[str],
    slug: str = "",
) -> dict:
    hit = first_92_entry(ticks, ttm_max=ttm_max)
    base = {
        "slug": slug,
        "series": series,
        "hit": False,
        "winner": winner,
        "leg": None,
        "ask": None,
        "ttm": None,
        "fill": None,
        "shares": None,
        "notional": None,
        "avg": None,
        "won": None,
        "pnl": None,
        "exit": None,
        "exit_reason": None,
        "exit_bid": None,
        "exit_ttm": None,
        "hedge_late": False,
        "winner_dump": False,
        "tick_n": len(ticks),
    }
    if hit is None:
        return base
    fill = fak_fill(series, float(hit["ask"]), hit.get("ask_size"))
    base.update(
        hit=True,
        leg=hit["leg"],
        ask=hit["ask"],
        ttm=hit["ttm"],
        fill=fill["status"],
        shares=None if fill["status"] == "zero" else round(fill["shares"], 4),
        notional=None if fill["status"] == "zero" else round(fill["notional"], 4),
        avg=None if fill["avg"] is None else round(float(fill["avg"]), 4),
    )
    if fill["status"] == "zero":
        base["exit"] = "zero"
        base["pnl"] = 0.0 if winner else None
        return base
    walker = walk_5m_held if series == "5m" else walk_15m_held
    settled = walker(ticks, hit, fill, winner)
    base.update(
        won=settled.get("won"),
        pnl=settled.get("pnl"),
        exit=settled.get("exit"),
        exit_reason=settled.get("exit_reason"),
        exit_bid=settled.get("exit_bid"),
        exit_ttm=settled.get("exit_ttm"),
        hedge_late=bool(settled.get("hedge_late")),
        winner_dump=bool(settled.get("winner_dump")),
    )
    return base


def summarize(rows: Sequence[dict]) -> dict:
    hits = [r for r in rows if r.get("hit")]
    decided = [r for r in hits if r.get("won") is not None or (
        r.get("pnl") is not None and r.get("exit") not in (None, "unresolved", "zero", "no_fill")
    )]
    # A hedge/dump sets won=False even on a winner dump. Count decided via pnl.
    with_pnl = [r for r in hits if r.get("pnl") is not None]
    wins = [r for r in with_pnl if r.get("exit") == "redeem_win"]
    pnl = [float(r["pnl"]) for r in with_pnl]
    return {
        "markets": len(rows),
        "hits": len(hits),
        "fills": sum(1 for r in hits if r.get("fill") in ("full", "partial")),
        "full": sum(1 for r in hits if r.get("fill") == "full"),
        "partial": sum(1 for r in hits if r.get("fill") == "partial"),
        "zero": sum(1 for r in hits if r.get("fill") == "zero"),
        "decided": len(with_pnl),
        "redeem_wins": sum(1 for r in hits if r.get("exit") == "redeem_win"),
        "redeem_losses": sum(1 for r in hits if r.get("exit") == "redeem_loss"),
        "hedges": sum(1 for r in hits if r.get("exit") == "hedge"),
        "dumps": sum(1 for r in hits if r.get("exit") == "dump"),
        "flattens": sum(1 for r in hits if r.get("exit") == "flatten"),
        "winner_dumps": sum(1 for r in hits if r.get("winner_dump")),
        "hedge_late": sum(1 for r in hits if r.get("hedge_late")),
        "unresolved": sum(1 for r in hits if r.get("exit") == "unresolved"),
        "win_rate": (len(wins) / len(with_pnl)) if with_pnl else None,
        "redeem_win_rate": (
            (sum(1 for r in with_pnl if r.get("exit") == "redeem_win") / len(with_pnl))
            if with_pnl else None
        ),
        "pnl_sum": round(sum(pnl), 4) if pnl else 0.0,
        "pnl_per_hit": round(sum(pnl) / len(with_pnl), 4) if with_pnl else None,
        "spend": round(sum(float(r["notional"]) for r in hits if r.get("notional")), 4),
        "misses": len(rows) - len(hits),
        "mean_ticks": (
            round(sum(int(r.get("tick_n") or 0) for r in rows) / len(rows), 1)
            if rows else 0.0
        ),
        "decided_n": len(decided),
    }


def iter_as_dicts(rows: Iterable[dict]) -> List[dict]:
    return list(rows)
