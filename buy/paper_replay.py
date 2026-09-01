"""Paper replay of live 5m / 15m entry + hedge gates on 1s books.

No orders. Does not import buybot*. Used by check_92c_week_backtest.py
and unit tests. Pathlog TOB is preferred; last-trade reconstruction is
the public-API fallback when ticks are not on disk.

Not replayed: Chainlink/PTB oracle, empty FAK 400s, POST RTT.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from buy.hedge_gate import (
    hedge_ladder_for_ttm,
    hedge_qualify_ok,
    held_hedge_decision,
)

GUI_SPREAD = 0.10
ASK_92_MIN = 0.92
ASK_92_MAX = 0.924999999999  # nearest cent 92¢ (0.915–0.925); 0.93 floats stay out
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


class HedgeSpec(NamedTuple):
    """Paper exit knobs. Live 5m / 15m defaults; sweep variants override.

    ``style``:
    - ``5m`` — persist / dump / fade / recovery / optional TTM ladder
    - ``15m`` — instant 35/40 + inverted 70/30 GUI
    - ``15m_then_5m_late`` — 15m inverted until ``late_ttm``, then 5m persist
    """

    name: str = "live"
    enabled: bool = True
    style: str = "5m"
    dump: float = FIVE_DUMP
    qualify: float = FIVE_QUALIFY
    ask_max: float = FIVE_ASK_MAX
    recovery: float = FIVE_RECOVERY
    persist_s: float = FIVE_PERSIST_S
    sell_fade: bool = True
    flatten: bool = True
    late_ttm: float = FIVE_LATE_TTM
    late_dump: float = FIVE_LATE_DUMP
    late_qualify: float = FIVE_LATE_QUALIFY
    late_ask_max: float = FIVE_LATE_ASK_MAX
    late_recovery: float = FIVE_LATE_RECOVERY
    fifteen_dump: float = FIFTEEN_THRESHOLD
    fifteen_ask_max: float = FIFTEEN_ASK_MAX
    # Informed persist vetoes (dumps still fire). 0 = off.
    min_drop_from_entry: float = 0.0
    lookback_s: float = 0.0
    min_drop_in_lookback: float = 0.0
    # Hindsight research only: persist-sell iff the path later proves lost.
    # ``crash`` = saw bid ≤ 40 after entry (live-legal). ``lost`` uses resolution.
    require_crash: bool = False
    require_lost: bool = False
    dump_min_s: float = 0.0  # bid must stay ≤ dump this many seconds


LIVE_FIVE = HedgeSpec(name="live_50_late30_58", style="5m")
LIVE_FIFTEEN = HedgeSpec(
    name="live_35_inverted",
    style="15m",
    dump=FIFTEEN_THRESHOLD,
    qualify=FIFTEEN_THRESHOLD,
    ask_max=FIFTEEN_ASK_MAX,
    recovery=FIFTEEN_ASK_MAX,
    persist_s=0.0,
    late_ttm=0.0,
)
RIDE = HedgeSpec(name="ride", enabled=False, style="5m")


def dump_only_spec(dump: float = 0.40, *, style: str = "5m") -> HedgeSpec:
    """Bid-only dump; persist book is a 0.1¢ band so 50/52 never sells."""
    dump_f = float(dump)
    qualify = min(dump_f + 0.001, 0.99)
    ask_max = min(qualify + 0.01, 1.0)
    recovery = min(ask_max + 0.01, 1.0)
    cents = int(round(dump_f * 100))
    return HedgeSpec(
        name=f"dump_{cents}",
        style=style,
        dump=dump_f,
        qualify=qualify,
        ask_max=ask_max,
        recovery=recovery,
        persist_s=1.0,
        sell_fade=True,
        flatten=False,
        late_ttm=0.0,
        late_dump=dump_f,
        late_qualify=qualify,
        late_ask_max=ask_max,
        late_recovery=recovery,
    )


def five_hedge_specs() -> List[HedgeSpec]:
    """92¢ 5m last-60s: same fills, different stops. Not live JSON."""
    return [
        RIDE,
        LIVE_FIVE,
        HedgeSpec(name="persist_50_no_late", late_ttm=0.0),
        HedgeSpec(
            name="persist_55_no_late",
            qualify=0.55, ask_max=0.58, recovery=0.60, late_ttm=0.0,
        ),
        HedgeSpec(
            name="persist_58_no_late",
            qualify=0.58, ask_max=0.60, recovery=0.62, late_ttm=0.0,
        ),
        HedgeSpec(
            name="persist_60_no_late",
            qualify=0.60, ask_max=0.62, recovery=0.65, late_ttm=0.0,
        ),
        HedgeSpec(
            name="persist_65_no_late",
            qualify=0.65, ask_max=0.68, recovery=0.70, late_ttm=0.0,
        ),
        HedgeSpec(
            name="persist_70_no_late",
            qualify=0.70, ask_max=0.72, recovery=0.75, late_ttm=0.0,
        ),
        HedgeSpec(name="late15_58", late_ttm=15.0),
        HedgeSpec(name="late45_58", late_ttm=45.0),
        HedgeSpec(name="late60_58", late_ttm=60.0),
        dump_only_spec(0.40),
        dump_only_spec(0.50),
        HedgeSpec(
            name="persist_50_late30_60",
            late_qualify=0.60, late_ask_max=0.62, late_recovery=0.65,
        ),
    ]


def fifteen_hedge_specs() -> List[HedgeSpec]:
    """92¢ 15m last-180s: live inverted vs last-minute 58/60 raise."""
    late58 = dict(
        late_dump=0.40, late_qualify=0.58, late_ask_max=0.60, late_recovery=0.62,
    )
    late60 = dict(
        late_dump=0.40, late_qualify=0.60, late_ask_max=0.62, late_recovery=0.65,
    )
    return [
        HedgeSpec(name="ride", enabled=False, style="15m"),
        LIVE_FIFTEEN,
        HedgeSpec(name="persist_50_like_5m", style="5m", late_ttm=0.0),
        HedgeSpec(name="persist_50_late60_58", style="5m", late_ttm=60.0, **late58),
        HedgeSpec(
            name="persist_60_like_5m",
            style="5m",
            qualify=0.60, ask_max=0.62, recovery=0.65, late_ttm=0.0,
        ),
        HedgeSpec(
            name="live_then_last60_58",
            style="15m_then_5m_late",
            late_ttm=60.0,
            **late58,
        ),
        HedgeSpec(
            name="live_then_last60_60",
            style="15m_then_5m_late",
            late_ttm=60.0,
            **late60,
        ),
        HedgeSpec(
            name="live_then_last90_58",
            style="15m_then_5m_late",
            late_ttm=90.0,
            **late58,
        ),
        dump_only_spec(0.40, style="5m"),
    ]


def breakeven_wr(win_pnl: float, lose_pnl: float) -> Optional[float]:
    """``p`` such that ``p*win + (1-p)*lose = 0``. ``lose`` is ≤ 0."""
    span = float(win_pnl) - float(lose_pnl)
    if abs(span) < 1e-12:
        return None
    return float(-lose_pnl) / span


def salvage_breakeven(shares: float, notional: float, salvage: float) -> Optional[float]:
    """BE win rate if every loser exits at ``salvage`` and no winner is sold."""
    if shares <= 0 or notional <= 0:
        return None
    win = shares - notional
    lose = shares * float(salvage) - notional
    return breakeven_wr(win, lose)


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


def size_caps(budget: float) -> Tuple[float, float]:
    """Spend / share rails for a paper clip. Live $2.50 keeps $3 / 5.

    A $10 probe uses the hourly-shaped $11 / 14 so the $3 live cap cannot
    shrink the FAK. Not a live JSON change.
    """
    b = float(budget)
    if abs(b - 2.5) < 1e-9:
        return BUY_MAX_SPEND, BUY_MAX_SHARES
    spend = round(b + 1.0, 2)
    shares = max(14.0, math.ceil(spend / 0.70) + 1.0)
    return spend, float(shares)


def fak_fill(
    series: str,
    ask: float,
    ask_size: Optional[float] = None,
    budget: float = BUDGET,
) -> dict:
    """Size the FAK. 5m posts at 92¢; 15m pins the limit to the 0.01 ask."""
    ask_f = float(ask)
    spend_cap, share_cap = size_caps(budget)
    if series == "15m":
        ask_f = round(ask_f + 1e-12, 2)
    if series == "5m":
        shares = quoted_buy_shares_up_to_limit(
            float(budget), ask_f, ASK_92_MIN, share_cap, spend_cap,
        )
        limit = ASK_92_MIN
        avg = limit
    else:
        shares = quoted_buy_shares(float(budget), ask_f, share_cap)
        limit = ask_f
        avg = ask_f
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
        by_sec.setdefault(sec, []).append(
            {"outcome": outcome, "px": px}
        )
    up_px = dn_px = None
    ticks: List[dict] = []
    t0 = int(start_ts)
    t1 = int(end_ts)
    for sec in range(t0, t1 + 1):
        for tr in by_sec.get(sec, []):
            if tr["outcome"] == "up":
                up_px = tr["px"]
            elif tr["outcome"] == "down":
                dn_px = tr["px"]
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
            }
        )
    return ticks


def print_size_near(
    trades: Sequence[dict],
    ts: float,
    outcome: str,
    *,
    px: float = 0.92,
    window_s: float = 0.0,
) -> float:
    """Sum of last-trade size at ~``px`` for ``outcome`` within ``window_s`` of ``ts``.

    This is flow at 92¢, not restable CLOB ask size. Used as a pessimistic
    fill cap, not as the default paper fill.
    """
    total = 0.0
    want = str(outcome or "").lower()
    for row in trades:
        row_ts = _f(row.get("ts") if "ts" in row else row.get("timestamp"))
        row_px = _f(row.get("px") if "px" in row else row.get("price"))
        if row_ts is None or row_px is None:
            continue
        if abs(row_ts - float(ts)) > float(window_s) + 1e-9:
            continue
        if str(row.get("outcome") or "").lower() != want:
            continue
        if abs(row_px - float(px)) >= 0.005 + 1e-12:
            continue
        total += float(row.get("size") or 0.0)
    return total
    if ask is None:
        return False
    # Nearest cent. 0.929999… is 93¢ (float 0.93), not 92.
    return abs(float(ask) - 0.92) < 0.005 + 1e-12


def ask_is_92(ask: Optional[float]) -> bool:
    if ask is None:
        return False
    # Nearest cent. 0.929999… is 93¢ (float 0.93), not 92.
    return abs(float(ask) - 0.92) < 0.005 + 1e-12


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


def _sell_result(
    *,
    shares: float,
    notional: float,
    px: float,
    bid_sz: Optional[float],
    ttm: Optional[float],
    winner: Optional[str],
    held: str,
    intent,
    late: bool,
) -> Optional[dict]:
    sell = shares
    if bid_sz is not None:
        sell = min(shares, bid_sz)
    sell = math.floor(sell * 10000 + 1e-12) / 10000
    if sell <= 0:
        return None
    proceeds = sell * float(px)
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
        "exit_bid": float(px),
        "exit_ttm": ttm,
        "won": False,
        "pnl": pnl,
        "hedge_late": bool(late),
        "winner_dump": bool(winner == held),
    }


def _five_intent_for_tick(
    row: dict,
    held: str,
    other: str,
    *,
    spec: HedgeSpec,
    ts: float,
    ttm: Optional[float],
    armed,
    persist_done: bool,
    toxic: bool,
    static_late: bool = False,
):
    bid, ask, last = _leg_quote(row, held)
    other_bid, other_ask, other_last = _leg_quote(row, other)
    dump = spec.late_dump if static_late else spec.dump
    qualify = spec.late_qualify if static_late else spec.qualify
    ask_max = spec.late_ask_max if static_late else spec.ask_max
    recovery = spec.late_recovery if static_late else spec.recovery
    late_ttm = 0.0 if static_late else spec.late_ttm
    ladder = hedge_ladder_for_ttm(
        ttm,
        dump,
        qualify,
        ask_max,
        recovery,
        late_ttm=late_ttm,
        late_dump=spec.late_dump,
        late_qualify=spec.late_qualify,
        late_ask_max=spec.late_ask_max,
        late_recovery=spec.late_recovery,
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
        persist_s=spec.persist_s,
        persist_done=persist_done,
        oracle_agrees=False,
        dump_ignore_oracle=True,
        dump_bid_max=dump,
        qualify_bid=qualify,
        qualify_ask_max=ask_max,
        recovery_cancel=recovery,
        sell_fade=spec.sell_fade,
        max_spread=FIVE_SPREAD,
        gui_ok=gui_ok,
        gui_why=gui_why,
        flatten=bool(spec.flatten and toxic),
        flatten_max=FIVE_FLATTEN_MAX,
        seconds_left=ttm,
        late_ttm=late_ttm,
        late_dump=spec.late_dump,
        late_qualify=spec.late_qualify,
        late_ask_max=spec.late_ask_max,
        late_recovery=spec.late_recovery,
    )
    return intent, ladder, bid


def walk_5m_held(
    ticks: Sequence[dict],
    hit: dict,
    fill: dict,
    winner: Optional[str],
    spec: Optional[HedgeSpec] = None,
) -> dict:
    """Live 5m dump / persist-1s / fade / recovery / last-30s ladder / flatten."""
    spec = spec or LIVE_FIVE
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
    if not spec.enabled:
        return _redeem(held, fill, winner)
    toxic = avg is not None and avg < FIVE_TOXIC_BELOW - 1e-12
    other = "down" if held == "up" else "up"
    armed = None
    persist_done = False
    for row in ticks:
        if not _after_entry(row, hit):
            continue
        ttm = _f(row.get("ttm"))
        ts = _f(row.get("ts"))
        if ts is None:
            continue
        intent, ladder, bid = _five_intent_for_tick(
            row, held, other,
            spec=spec, ts=ts, ttm=ttm, armed=armed,
            persist_done=persist_done, toxic=toxic,
        )
        persist_done = bool(intent.persist_done)
        armed = intent.persist_ts
        if intent.action not in {"sell", "dump"}:
            continue
        px = float(intent.sell_at) if intent.sell_at is not None else bid
        if px is None:
            continue
        sold = _sell_result(
            shares=shares, notional=notional, px=px,
            bid_sz=_f(row.get("ubs" if held == "up" else "dbs")),
            ttm=ttm, winner=winner, held=held, intent=intent,
            late=bool(ladder.late),
        )
        if sold is not None:
            if _informed_blocks_sell(spec, hit, ticks, row, px, intent, winner, held):
                continue
            return sold
    return _redeem(held, fill, winner)


def _bid_at_or_before(ticks: Sequence[dict], held: str, ts: float) -> Optional[float]:
    best = None
    best_ts = None
    for row in ticks:
        row_ts = _f(row.get("ts"))
        if row_ts is None or row_ts > ts + 1e-12:
            continue
        bid, _, _ = _leg_quote(row, held)
        if bid is None:
            continue
        if best_ts is None or row_ts >= best_ts:
            best_ts = row_ts
            best = bid
    return best


def path_after_entry(ticks: Sequence[dict], hit: dict, held: str) -> dict:
    """Watcher features after the 92¢ fill. No resolution leak except end_bid."""
    bids: List[Tuple[float, float, Optional[float], Optional[float]]] = []
    for row in ticks:
        if not _after_entry(row, hit):
            continue
        bid, ask, last = _leg_quote(row, held)
        ttm = _f(row.get("ttm"))
        if bid is None:
            continue
        bids.append((bid, float(ttm) if ttm is not None else float("nan"), ask, last))
    if not bids:
        return {
            "n": 0,
            "min_bid": None,
            "max_bid": None,
            "end_bid": None,
            "sec_le52": 0,
            "sec_le40": 0,
            "first_le52_ttm": None,
            "recovered_70_after_52": False,
            "drop_from_entry": None,
        }
    min_bid = min(b[0] for b in bids)
    max_bid = max(b[0] for b in bids)
    end_bid = bids[-1][0]
    sec_le52 = sum(1 for b in bids if b[0] <= 0.52 + 1e-12)
    sec_le40 = sum(1 for b in bids if b[0] <= 0.40 + 1e-12)
    first_le52 = next((b[1] for b in bids if b[0] <= 0.52 + 1e-12), None)
    saw_52 = False
    recovered = False
    for bid, _ttm, _ask, _last in bids:
        if bid <= 0.52 + 1e-12:
            saw_52 = True
        elif saw_52 and bid >= 0.70 - 1e-12:
            recovered = True
    entry = _f(hit.get("ask")) or ASK_92_MIN
    return {
        "n": len(bids),
        "min_bid": round(min_bid, 4),
        "max_bid": round(max_bid, 4),
        "end_bid": round(end_bid, 4),
        "sec_le52": sec_le52,
        "sec_le40": sec_le40,
        "first_le52_ttm": None if first_le52 is None or first_le52 != first_le52 else round(float(first_le52), 1),
        "recovered_70_after_52": recovered,
        "drop_from_entry": round(entry - min_bid, 4),
    }


def _informed_blocks_sell(
    spec: HedgeSpec,
    hit: dict,
    ticks: Sequence[dict],
    row: dict,
    px: float,
    intent,
    winner: Optional[str],
    held: str,
) -> bool:
    """True = hold instead of this persist/dump. Dumps ignore drop/crash vetoes."""
    if spec.require_lost and winner == held:
        return True
    dump = bool(intent.dump)
    if spec.dump_min_s > 0 and dump:
        ts = _f(row.get("ts")) or 0.0
        so_far = [
            t for t in ticks
            if _f(t.get("ts")) is None or _f(t.get("ts")) <= ts + 1e-12
        ]
        feats = path_after_entry(so_far, hit, held)
        if float(feats.get("sec_le40") or 0) + 1e-12 < float(spec.dump_min_s):
            return True
    if dump:
        return False
    if spec.require_crash:
        feats = path_after_entry(ticks, hit, held)
        # Only the path *so far* (ticks at/before this row).
        so_far = [t for t in ticks if _f(t.get("ts")) is None or _f(t.get("ts")) <= (_f(row.get("ts")) or 0) + 1e-12]
        feats = path_after_entry(so_far, hit, held)
        if feats.get("min_bid") is None or float(feats["min_bid"]) > 0.40 + 1e-12:
            return True
    entry = _f(hit.get("ask")) or ASK_92_MIN
    if spec.min_drop_from_entry > 0 and (entry - float(px)) < spec.min_drop_from_entry - 1e-12:
        return True
    if spec.lookback_s > 0 and spec.min_drop_in_lookback > 0:
        ts = _f(row.get("ts"))
        if ts is None:
            return True
        past = _bid_at_or_before(ticks, held, ts - float(spec.lookback_s))
        if past is None or (past - float(px)) < spec.min_drop_in_lookback - 1e-12:
            return True
    return False


def informed_five_specs() -> List[HedgeSpec]:
    """Watcher-style 5m stops. Dumps stay 40. Not live JSON."""
    return [
        RIDE,
        LIVE_FIVE,
        HedgeSpec(name="persist_50_no_late", late_ttm=0.0),
        HedgeSpec(name="persist_50_3s_no_late", persist_s=3.0, late_ttm=0.0),
        HedgeSpec(name="persist_50_5s_no_late", persist_s=5.0, late_ttm=0.0),
        dump_only_spec(0.40),
        HedgeSpec(
            name="collapse_15c_3s",
            late_ttm=0.0,
            lookback_s=3.0,
            min_drop_in_lookback=0.15,
        ),
        HedgeSpec(
            name="collapse_20c_5s",
            late_ttm=0.0,
            lookback_s=5.0,
            min_drop_in_lookback=0.20,
        ),
        HedgeSpec(
            name="drop_40_from_entry",
            late_ttm=0.0,
            min_drop_from_entry=0.40,
        ),
        HedgeSpec(name="crash_then_persist_50", late_ttm=0.0, require_crash=True),
        HedgeSpec(name="dump40_hold_1s", late_ttm=0.0, persist_s=5.0, dump_min_s=1.0),
        HedgeSpec(name="dump40_hold_2s", late_ttm=0.0, persist_s=5.0, dump_min_s=2.0),
        HedgeSpec(
            name="persist3s_dump2s",
            persist_s=3.0, late_ttm=0.0, dump_min_s=2.0,
        ),
        HedgeSpec(name="hindsight_lost_only", require_lost=True),
        HedgeSpec(name="hindsight_lost_at_50", late_ttm=0.0, require_lost=True),
    ]


def walk_15m_held(
    ticks: Sequence[dict],
    hit: dict,
    fill: dict,
    winner: Optional[str],
    spec: Optional[HedgeSpec] = None,
) -> dict:
    """Stopped 15m: toxic dump at ≤35¢; else 35/40 book + inverted 70/30 GUI.

    ``15m_then_5m_late`` keeps that inverted book until ``late_ttm``, then
    switches to 5m persist (58/60 or 60/62). Raising 15m's 35/40 threshold
    to 60 without dropping 70/30 GUI never sells at 60 — held GUI max is 30¢.
    """
    spec = spec or LIVE_FIFTEEN
    if spec.style == "5m":
        return walk_5m_held(ticks, hit, fill, winner, spec=spec)
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
    if not spec.enabled:
        return _redeem(held, fill, winner)
    toxic = avg is not None and avg < FIFTEEN_TOXIC_BELOW - 1e-12
    other = "down" if held == "up" else "up"
    dump_px = float(spec.fifteen_dump)
    ask_max = float(spec.fifteen_ask_max)
    armed = None
    persist_done = False
    for row in ticks:
        if not _after_entry(row, hit):
            continue
        bid, ask, last = _leg_quote(row, held)
        other_bid, other_ask, other_last = _leg_quote(row, other)
        ttm = _f(row.get("ttm"))
        ts = _f(row.get("ts"))
        if (
            spec.style == "15m_then_5m_late"
            and spec.late_ttm > 0
            and ttm is not None
            and ttm <= spec.late_ttm + 1e-12
            and ts is not None
        ):
            intent, ladder, px_bid = _five_intent_for_tick(
                row, held, other,
                spec=spec, ts=ts, ttm=ttm, armed=armed,
                persist_done=persist_done, toxic=toxic, static_late=True,
            )
            persist_done = bool(intent.persist_done)
            armed = intent.persist_ts
            if intent.action not in {"sell", "dump"}:
                continue
            px = float(intent.sell_at) if intent.sell_at is not None else px_bid
            if px is None:
                continue
            sold = _sell_result(
                shares=shares, notional=notional, px=px,
                bid_sz=_f(row.get("ubs" if held == "up" else "dbs")),
                ttm=ttm, winner=winner, held=held, intent=intent,
                late=True,
            )
            if sold is not None:
                return sold
            continue
        if bid is None:
            continue
        if toxic and bid <= dump_px + 1e-12:
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
            bid, ask, dump_px, FIFTEEN_SPREAD, ask_max,
        )
        if not ok:
            continue
        gui_ok, gui_why = hedge_consensus_ok(
            bid, ask, last,
            other_bid, other_ask, other_last,
            held_gui_max=MAX_LOSER_BID,
            other_gui_min=MIN_WINNER_BID,
            min_edge=MIN_BID_EDGE,
            last_trade_max=ask_max,
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
    budget: float = BUDGET,
    hedge: Optional[HedgeSpec] = None,
    ask_size: Optional[float] = None,
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
    spec = hedge or (LIVE_FIVE if series == "5m" else LIVE_FIFTEEN)
    size = ask_size if ask_size is not None else hit.get("ask_size")
    fill = fak_fill(series, float(hit["ask"]), size, budget=budget)
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
    walker = walk_5m_held if spec.style == "5m" else walk_15m_held
    settled = walker(ticks, hit, fill, winner, spec=spec)
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
    filled = [r for r in hits if r.get("fill") in ("full", "partial")]
    with_pnl = [r for r in filled if r.get("pnl") is not None]
    redeem_wins = [r for r in filled if r.get("exit") == "redeem_win"]
    resolution_wins = [
        r for r in filled
        if r.get("winner") in ("up", "down") and r.get("winner") == r.get("leg")
    ]
    resolved = [
        r for r in filled
        if r.get("winner") in ("up", "down")
    ]
    pnl = [float(r["pnl"]) for r in with_pnl]
    return {
        "markets": len(rows),
        "hits": len(hits),
        "fills": len(filled),
        "full": sum(1 for r in hits if r.get("fill") == "full"),
        "partial": sum(1 for r in hits if r.get("fill") == "partial"),
        "zero": sum(1 for r in hits if r.get("fill") == "zero"),
        "decided": len(with_pnl),
        "redeem_wins": len(redeem_wins),
        "redeem_losses": sum(1 for r in filled if r.get("exit") == "redeem_loss"),
        "hedges": sum(1 for r in filled if r.get("exit") == "hedge"),
        "dumps": sum(1 for r in filled if r.get("exit") == "dump"),
        "flattens": sum(1 for r in filled if r.get("exit") == "flatten"),
        "winner_dumps": sum(1 for r in filled if r.get("winner_dump")),
        "hedge_late": sum(1 for r in filled if r.get("hedge_late")),
        "unresolved": sum(1 for r in filled if r.get("exit") == "unresolved"),
        "win_rate": (len(redeem_wins) / len(with_pnl)) if with_pnl else None,
        "redeem_win_rate": (
            (len(redeem_wins) / len(with_pnl)) if with_pnl else None
        ),
        "resolution_win_rate": (
            (len(resolution_wins) / len(resolved)) if resolved else None
        ),
        "pnl_sum": round(sum(pnl), 4) if pnl else 0.0,
        "pnl_per_hit": round(sum(pnl) / len(with_pnl), 4) if with_pnl else None,
        "spend": round(sum(float(r["notional"]) for r in filled if r.get("notional")), 4),
        "misses": len(rows) - len(hits),
        "mean_ticks": (
            round(sum(int(r.get("tick_n") or 0) for r in rows) / len(rows), 1)
            if rows else 0.0
        ),
        "decided_n": len(with_pnl),
    }


def iter_as_dicts(rows: Iterable[dict]) -> List[dict]:
    return list(rows)
