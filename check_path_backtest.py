#!/usr/bin/env python3
"""Query recorded market paths: what if we entered at price X with Y time left?

Reads pathlog/ticks/*.jsonl written by pathlog.py. Does not place orders.

pathlog auto-prunes ticks (14 days / 400 MB). Export regularly — pruned
JSONL is deleted. Copy CSVs (or the ticks dir) off the VM.

Ticks include top-of-book price and displayed size (ub/ua/db/da and
ubs/uas/dbs/das). Older JSONL without size is still readable: the
backtest treats missing size as infinite liquidity at the best ask.

Examples (on the VM):
  python check_path_backtest.py --ask-min 0.80 --ask-max 0.85 --ttm-max 120
  python check_path_backtest.py --grid --budget 2.5 --series 5m
  python check_path_backtest.py --grid --budget 15 --series 5m
  python check_path_backtest.py --ask-min 0.75 --ask-max 0.90 --ttm-max 120 --budget 15 --series 5m --csv /tmp/hits_15.csv
  python check_path_backtest.py --anatomy --series 5m --ttm-max 120
  python check_path_backtest.py --compare --series 5m --budget 2.5
  python check_path_backtest.py --export-market btc-updown-5m-1786528500 --csv /tmp/m.csv
  python check_path_backtest.py --ask-min 0.98 --ask-max 0.99 --ttm-max 90 --csv /tmp/hits.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent
TICK_DIR = REPO / "pathlog" / "ticks"


@dataclass
class MarketPath:
    slug: str
    series: str
    start_ts: float
    end_ts: float
    question: str
    ticks: List[dict] = field(default_factory=list)
    winner: Optional[str] = None


def load_market_file(path: Path) -> Optional[MarketPath]:
    header: Optional[dict] = None
    ticks: List[dict] = []
    winner: Optional[str] = None
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = row.get("e")
                if event == "open":
                    header = row
                elif event == "tick":
                    ticks.append(row)
                elif event == "resolved":
                    w = str(row.get("winner") or "").lower()
                    if w in ("up", "down"):
                        winner = w
    except OSError:
        return None
    if not header:
        return None
    slug = str(header.get("slug") or path.stem)
    return MarketPath(
        slug=slug,
        series=str(header.get("series") or ""),
        start_ts=float(header.get("start") or 0),
        end_ts=float(header.get("end") or 0),
        question=str(header.get("q") or ""),
        ticks=ticks,
        winner=winner,
    )


def infer_winner(ticks: Sequence[dict]) -> Optional[str]:
    """Last complete tick: the side whose ask is clearly decided."""
    for row in reversed(ticks):
        ua = _f(row.get("ua"))
        da = _f(row.get("da"))
        if ua is None or da is None:
            continue
        if ua >= 0.95 and da <= 0.05:
            return "up"
        if da >= 0.95 and ua <= 0.05:
            return "down"
        if ua > da + 0.4:
            return "up"
        if da > ua + 0.4:
            return "down"
        break
    return None


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first_entry(
    ticks: Sequence[dict],
    *,
    ask_min: float,
    ask_max: float,
    ttm_min: float,
    ttm_max: float,
    max_spread: Optional[float] = None,
) -> Optional[dict]:
    """First tick where a leg's ask is in [ask_min, ask_max] and ttm in range.

    Picks the higher ask if both legs qualify (should be rare).
    ``ask_size`` is None on legacy ticks that omitted displayed size.
    """
    for row in ticks:
        ttm = _f(row.get("ttm"))
        if ttm is None or not (ttm_min <= ttm <= ttm_max):
            continue
        candidates: List[Tuple[str, float, Optional[float], Optional[float]]] = []
        for leg, ask_key, bid_key, ask_sz_key in (
            ("up", "ua", "ub", "uas"),
            ("down", "da", "db", "das"),
        ):
            ask = _f(row.get(ask_key))
            if ask is None or not (ask_min - 1e-12 <= ask <= ask_max + 1e-12):
                continue
            bid = _f(row.get(bid_key))
            if max_spread is not None and bid is not None and (ask - bid) > max_spread + 1e-12:
                continue
            if max_spread is not None and bid is None:
                continue
            ask_size = _f(row.get(ask_sz_key))
            candidates.append((leg, ask, bid, ask_size))
        if not candidates:
            continue
        leg, ask, bid, ask_size = max(candidates, key=lambda item: item[1])
        return {
            "ts": row.get("ts"),
            "ttm": ttm,
            "leg": leg,
            "ask": ask,
            "bid": bid,
            "ask_size": ask_size,
        }
    return None


# Pathlog has no last-trade. Tight books use mid (same 10¢ GUI rule as the bots);
# wide books fall back to the ask so |up-dn| still means "coin flip vs decided".
PATHLOG_GUI_SPREAD = 0.10

# Named alternatives vs the live 5m probe. Same ticks, no live orders.
COMPARE_PRESETS: List[Tuple[str, float, float, float]] = [
    ("live_5m", 0.75, 0.90, 120.0),
    ("window_180s", 0.75, 0.90, 180.0),
    ("window_240s", 0.75, 0.90, 240.0),
    ("whole_5m", 0.75, 0.90, 300.0),
    ("band_70_90", 0.70, 0.90, 120.0),
    ("band_75_95", 0.75, 0.95, 120.0),
]


def path_display_price(bid: Any, ask: Any) -> Optional[float]:
    bid_f = _f(bid)
    ask_f = _f(ask)
    if bid_f is not None and ask_f is not None:
        if ask_f < bid_f:
            return None
        if (ask_f - bid_f) <= PATHLOG_GUI_SPREAD + 1e-12:
            return (bid_f + ask_f) / 2.0
    return ask_f


def tick_sides(row: dict, min_edge: float) -> Optional[dict]:
    ttm = _f(row.get("ttm"))
    ua = _f(row.get("ua"))
    da = _f(row.get("da"))
    if ttm is None or ua is None or da is None:
        return None
    up_gui = path_display_price(row.get("ub"), ua)
    dn_gui = path_display_price(row.get("db"), da)
    if up_gui is None:
        up_gui = ua
    if dn_gui is None:
        dn_gui = da
    edge = abs(up_gui - dn_gui)
    if up_gui > dn_gui:
        winning = "up"
        win_ask = ua
    elif dn_gui > up_gui:
        winning = "down"
        win_ask = da
    else:
        winning = None
        win_ask = None
    return {
        "ttm": ttm,
        "up_gui": up_gui,
        "dn_gui": dn_gui,
        "ua": ua,
        "da": da,
        "edge": edge,
        "winning": winning,
        "win_ask": win_ask,
        "ambiguous": edge < float(min_edge) - 1e-12 or winning is None,
    }


def classify_window(
    ticks: Sequence[dict],
    *,
    ttm_max: float = 120.0,
    min_edge: float = 0.05,
    ask_min: float = 0.75,
    ask_max: float = 0.90,
) -> dict:
    """Was the book already decided before the buy window, or tight until the end?

    ``decided_before_*`` — last tick with ttm > ttm_max already had ≥ min_edge.
    ``tight_through_window`` — every in-window tick stayed inside min_edge.
    ``cleared_in_window`` — first became unambiguous only after the window opened.
    """
    pre: List[dict] = []
    window: List[dict] = []
    for row in ticks:
        snap = tick_sides(row, min_edge)
        if snap is None:
            continue
        if snap["ttm"] > float(ttm_max) + 1e-12:
            pre.append(snap)
        elif snap["ttm"] > 0:
            window.append(snap)

    at_open = pre[-1] if pre else None
    decided_before = bool(
        at_open and not at_open["ambiguous"] and at_open.get("winning")
    )
    in_window_clear = [
        snap for snap in window
        if not snap["ambiguous"] and snap.get("winning")
    ]
    in_band = [
        snap for snap in in_window_clear
        if snap["win_ask"] is not None
        and float(ask_min) - 1e-12 <= float(snap["win_ask"]) <= float(ask_max) + 1e-12
    ]

    if not window and not pre:
        bucket = "no_ticks"
    elif not window:
        bucket = "no_window_ticks"
    elif decided_before:
        win_ask = at_open["win_ask"] if at_open else None
        if win_ask is None:
            bucket = "decided_before_in_band"
        elif float(win_ask) > float(ask_max) + 1e-12:
            bucket = "decided_before_above_band"
        elif float(win_ask) < float(ask_min) - 1e-12:
            bucket = "decided_before_below_band"
        else:
            bucket = "decided_before_in_band"
    elif not in_window_clear:
        bucket = "tight_through_window"
    else:
        bucket = "cleared_in_window"

    first_clear = in_window_clear[0] if in_window_clear else None
    first_band = in_band[0] if in_band else None
    amb_n = sum(1 for snap in window if snap["ambiguous"])
    return {
        "bucket": bucket,
        "open_ttm": None if at_open is None else at_open["ttm"],
        "open_edge": None if at_open is None else round(float(at_open["edge"]), 4),
        "open_win_ask": None if at_open is None else at_open.get("win_ask"),
        "open_winning": None if at_open is None else at_open.get("winning"),
        "window_ticks": len(window),
        "ambiguous_ticks": amb_n,
        "first_clear_ttm": None if first_clear is None else first_clear["ttm"],
        "first_in_band_ttm": None if first_band is None else first_band["ttm"],
        "first_in_band_ask": None if first_band is None else first_band.get("win_ask"),
        "first_in_band_leg": None if first_band is None else first_band.get("winning"),
    }


def anatomy_rows(
    markets: Sequence[MarketPath],
    *,
    ttm_max: float,
    min_edge: float,
    ask_min: float,
    ask_max: float,
) -> List[dict]:
    rows: List[dict] = []
    for market in markets:
        row = classify_window(
            market.ticks,
            ttm_max=ttm_max,
            min_edge=min_edge,
            ask_min=ask_min,
            ask_max=ask_max,
        )
        winner = market.winner or infer_winner(market.ticks)
        row.update(
            {
                "slug": market.slug,
                "series": market.series,
                "winner": winner,
            }
        )
        rows.append(row)
    return rows


def summarize_anatomy(rows: Sequence[dict]) -> dict:
    buckets: Dict[str, int] = {}
    for row in rows:
        name = str(row.get("bucket") or "unknown")
        buckets[name] = buckets.get(name, 0) + 1
    return {"markets": len(rows), "buckets": buckets}


MIN_FAK_SHARES = 0.01
MIN_FAK_BUDGET = 0.01


def simulate_fak_buy(
    budget: float,
    ask: float,
    ask_size: Optional[float] = None,
) -> dict:
    """Limit FAK at the quoted ask — recorded levels only.

    Live bots post ``budget/ask`` shares at that ask (displayed size is not a
    cap). This helper models **fillable** size: ``min(budget/ask, ask_size)``,
    no walking into cheaper asks. ``ask_size is None`` (legacy ticks) =
    infinite size at ``ask``, same as the old full-fill-at-best-ask P&L.
    """
    out = {
        "status": "zero",
        "shares": 0.0,
        "notional": 0.0,
        "avg": None,
        "ask": ask,
        "ask_size": ask_size,
        "legacy": ask_size is None,
    }
    if budget < MIN_FAK_BUDGET or ask <= 0:
        return out
    wanted = budget / ask
    if ask_size is None:
        out.update(
            {
                "status": "full",
                "shares": wanted,
                "notional": budget,
                "avg": ask,
            }
        )
        return out
    if ask_size < MIN_FAK_SHARES:
        return out
    raw = min(wanted, ask_size)
    # CLOB taker amounts allow at most four decimal places (live quoted_buy_shares).
    shares = math.floor(raw * 10000 + 1e-12) / 10000
    if shares < MIN_FAK_SHARES:
        return out
    notional = shares * ask
    avg = notional / shares if shares else None
    if ask_size + 1e-12 >= wanted:
        status = "full"
    else:
        status = "partial"
    out.update(
        {
            "status": status,
            "shares": shares,
            "notional": notional,
            "avg": avg,
        }
    )
    return out


def hypothetical_pnl(ask: float, won: bool, budget: float) -> float:
    if ask <= 0 or budget <= 0:
        return 0.0
    shares = budget / ask
    if won:
        return shares * 1.0 - budget
    return -budget


def fill_pnl(fill: dict, won: bool) -> float:
    notional = float(fill.get("notional") or 0.0)
    shares = float(fill.get("shares") or 0.0)
    if notional <= 0 or shares <= 0:
        return 0.0
    if won:
        return shares - notional
    return -notional


def iter_markets(tick_dir: Path = TICK_DIR) -> Iterable[MarketPath]:
    if not tick_dir.exists():
        return
    for path in sorted(tick_dir.glob("*.jsonl")):
        market = load_market_file(path)
        if market:
            yield market


def evaluate_rule(
    markets: Sequence[MarketPath],
    *,
    ask_min: float,
    ask_max: float,
    ttm_min: float,
    ttm_max: float,
    budget: float,
    max_spread: Optional[float] = None,
) -> List[dict]:
    rows: List[dict] = []
    for market in markets:
        winner = market.winner or infer_winner(market.ticks)
        hit = first_entry(
            market.ticks,
            ask_min=ask_min,
            ask_max=ask_max,
            ttm_min=ttm_min,
            ttm_max=ttm_max,
            max_spread=max_spread,
        )
        if hit is None:
            rows.append(
                {
                    "slug": market.slug,
                    "series": market.series,
                    "hit": False,
                    "winner": winner,
                    "leg": None,
                    "ask": None,
                    "ask_size": None,
                    "ttm": None,
                    "fill": None,
                    "shares": None,
                    "notional": None,
                    "avg": None,
                    "won": None,
                    "pnl": None,
                }
            )
            continue
        fill = simulate_fak_buy(budget, float(hit["ask"]), hit.get("ask_size"))
        filled = fill["status"] != "zero"
        won = winner is not None and filled and hit["leg"] == winner
        if not filled:
            pnl = 0.0 if winner else None
            won_out: Optional[bool] = None
        else:
            pnl = fill_pnl(fill, bool(won)) if winner else None
            won_out = won if winner else None
        rows.append(
            {
                "slug": market.slug,
                "series": market.series,
                "hit": True,
                "winner": winner,
                "leg": hit["leg"],
                "ask": hit["ask"],
                "ask_size": hit.get("ask_size"),
                "ttm": hit["ttm"],
                "fill": fill["status"],
                "shares": None if not filled else round(fill["shares"], 4),
                "notional": None if not filled else round(fill["notional"], 4),
                "avg": None if fill["avg"] is None else round(float(fill["avg"]), 4),
                "won": won_out,
                "pnl": None if pnl is None else round(pnl, 4),
            }
        )
    return rows


def summarize(rows: Sequence[dict]) -> dict:
    hits = [r for r in rows if r.get("hit")]
    decided = [r for r in hits if r.get("won") is not None]
    wins = [r for r in decided if r.get("won")]
    pnl = [float(r["pnl"]) for r in decided if r.get("pnl") is not None]
    full = sum(1 for r in hits if r.get("fill") == "full")
    partial = sum(1 for r in hits if r.get("fill") == "partial")
    zero = sum(1 for r in hits if r.get("fill") == "zero")
    filled = [r for r in hits if r.get("fill") in ("full", "partial")]
    notionals = [float(r["notional"]) for r in filled if r.get("notional") is not None]
    avgs = [float(r["avg"]) for r in filled if r.get("avg") is not None]
    return {
        "markets": len(rows),
        "hits": len(hits),
        "decided": len(decided),
        "wins": len(wins),
        "win_rate": (len(wins) / len(decided)) if decided else None,
        "pnl_sum": round(sum(pnl), 4) if pnl else None,
        "misses": len(rows) - len(hits),
        "unresolved": sum(1 for r in hits if r.get("winner") is None),
        "full": full,
        "partial": partial,
        "zero": zero,
        "avg_notional": round(sum(notionals) / len(notionals), 4) if notionals else None,
        "avg_fill_px": round(sum(avgs) / len(avgs), 4) if avgs else None,
    }


def grid_scan(
    markets: Sequence[MarketPath],
    *,
    budget: float,
    max_spread: Optional[float],
) -> List[dict]:
    asks = [0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.98]
    ttms = [30, 60, 90, 120, 180, 240]
    out: List[dict] = []
    for ask in asks:
        for ttm_max in ttms:
            rows = evaluate_rule(
                markets,
                ask_min=ask,
                ask_max=min(0.99, ask + 0.02),
                ttm_min=0.0,
                ttm_max=float(ttm_max),
                budget=budget,
                max_spread=max_spread,
            )
            stats = summarize(rows)
            out.append({"ask": ask, "ttm_max": ttm_max, **stats})
    return out


def export_market_csv(market: MarketPath, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["ts", "ttm_s", "up_bid", "up_ask", "dn_bid", "dn_ask", "up_bid_sz", "up_ask_sz", "dn_bid_sz", "dn_ask_sz"]
        )
        for row in market.ticks:
            writer.writerow(
                [
                    row.get("ts"),
                    row.get("ttm"),
                    row.get("ub"),
                    row.get("ua"),
                    row.get("db"),
                    row.get("da"),
                    row.get("ubs"),
                    row.get("uas"),
                    row.get("dbs"),
                    row.get("das"),
                ]
            )


def export_anatomy_csv(rows: Sequence[dict], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "slug",
        "series",
        "winner",
        "bucket",
        "open_ttm",
        "open_edge",
        "open_win_ask",
        "open_winning",
        "window_ticks",
        "ambiguous_ticks",
        "first_clear_ttm",
        "first_in_band_ttm",
        "first_in_band_ask",
        "first_in_band_leg",
    ]
    with open(dest, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def export_hits_csv(rows: Sequence[dict], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "slug",
        "series",
        "hit",
        "leg",
        "ask",
        "ask_size",
        "ttm",
        "fill",
        "shares",
        "notional",
        "avg",
        "winner",
        "won",
        "pnl",
    ]
    with open(dest, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _print_summary(stats: dict) -> None:
    wr = stats["win_rate"]
    wr_s = f"{wr:.1%}" if wr is not None else "n/a"
    print(
        f"markets={stats['markets']}  hits={stats['hits']}  "
        f"full={stats.get('full', 0)}  partial={stats.get('partial', 0)}  "
        f"zero={stats.get('zero', 0)}  decided={stats['decided']}  "
        f"wins={stats['wins']}  win_rate={wr_s}  pnl={stats['pnl_sum']}  "
        f"misses={stats['misses']}  unresolved={stats['unresolved']}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest entry rules against pathlog ticks")
    ap.add_argument("--dir", type=Path, default=TICK_DIR)
    ap.add_argument("--ask-min", type=float, default=0.80)
    ap.add_argument("--ask-max", type=float, default=0.99)
    ap.add_argument("--ttm-min", type=float, default=0.0, help="seconds left, inclusive")
    ap.add_argument("--ttm-max", type=float, default=120.0, help="seconds left, inclusive")
    ap.add_argument("--budget", type=float, default=2.5)
    ap.add_argument("--max-spread", type=float, default=None)
    ap.add_argument("--series", default="", help="substring filter, e.g. 5m")
    ap.add_argument("--csv", type=Path, default=None, help="write per-market hit or anatomy rows")
    ap.add_argument("--export-market", default="", help="slug to dump as tick CSV")
    ap.add_argument("--grid", action="store_true", help="print ask × ttm_max win-rate table")
    ap.add_argument(
        "--anatomy",
        action="store_true",
        help="classify each market: decided before window vs tight through the window",
    )
    ap.add_argument(
        "--compare",
        action="store_true",
        help="score named alternative windows/bands on the same ticks (no live orders)",
    )
    ap.add_argument(
        "--min-edge",
        type=float,
        default=0.05,
        help="GUI/ask gap that counts as unambiguous (anatomy; default 5¢)",
    )
    args = ap.parse_args(argv)

    markets = list(iter_markets(args.dir))
    if args.series:
        needle = args.series.lower()
        markets = [m for m in markets if needle in m.series.lower() or needle in m.slug.lower()]

    if args.export_market:
        slug = args.export_market
        match = next((m for m in markets if m.slug == slug or m.slug.endswith(slug)), None)
        if match is None:
            print(f"no recorded market matching {slug}", file=sys.stderr)
            return 1
        dest = args.csv or Path(f"/tmp/{match.slug}.csv")
        export_market_csv(match, dest)
        print(f"wrote {len(match.ticks)} ticks → {dest}")
        return 0

    if args.grid:
        table = grid_scan(markets, budget=args.budget, max_spread=args.max_spread)
        print("ask\tttm_max\thits\tfull\tpartial\tzero\tdecided\twins\twin_rate\tpnl")
        for row in table:
            wr = row["win_rate"]
            wr_s = f"{wr:.3f}" if wr is not None else ""
            print(
                f"{row['ask']:.2f}\t{row['ttm_max']}\t{row['hits']}\t"
                f"{row.get('full', 0)}\t{row.get('partial', 0)}\t{row.get('zero', 0)}\t"
                f"{row['decided']}\t{row['wins']}\t{wr_s}\t{row['pnl_sum']}"
            )
        return 0

    if args.anatomy:
        ask_min, ask_max = args.ask_min, args.ask_max
        if ask_min == 0.80 and ask_max == 0.99:
            ask_min, ask_max = 0.75, 0.90
        rows = anatomy_rows(
            markets,
            ttm_max=args.ttm_max,
            min_edge=args.min_edge,
            ask_min=ask_min,
            ask_max=ask_max,
        )
        stats = summarize_anatomy(rows)
        print(
            f"markets={stats['markets']}  ttm_max={args.ttm_max:g}  "
            f"min_edge={args.min_edge:g}  band={ask_min:g}-{ask_max:g}"
        )
        order = [
            "decided_before_in_band",
            "decided_before_above_band",
            "decided_before_below_band",
            "tight_through_window",
            "cleared_in_window",
            "no_window_ticks",
            "no_ticks",
        ]
        buckets = stats["buckets"]
        for name in order:
            if name in buckets:
                print(f"  {buckets[name]:4d}  {name}")
        for name, n in sorted(buckets.items()):
            if name not in order:
                print(f"  {n:4d}  {name}")
        print(
            "decided_before_* = already unambiguous when the last-120s window opened.\n"
            "tight_through_window = stayed inside min_edge until expiry.\n"
            "cleared_in_window = first became a clear side only after the window opened."
        )
        if args.csv:
            export_anatomy_csv(rows, args.csv)
            print(f"wrote {len(rows)} rows → {args.csv}")
        return 0

    if args.compare:
        print("preset\task_min\task_max\tttm_max\thits\tfull\tpartial\tzero\tdecided\twins\twin_rate\tpnl")
        for name, ask_min, ask_max, ttm_max in COMPARE_PRESETS:
            rows = evaluate_rule(
                markets,
                ask_min=ask_min,
                ask_max=ask_max,
                ttm_min=0.0,
                ttm_max=ttm_max,
                budget=args.budget,
                max_spread=args.max_spread,
            )
            stats = summarize(rows)
            wr = stats["win_rate"]
            wr_s = f"{wr:.3f}" if wr is not None else ""
            print(
                f"{name}\t{ask_min:.2f}\t{ask_max:.2f}\t{ttm_max:g}\t"
                f"{stats['hits']}\t{stats.get('full', 0)}\t{stats.get('partial', 0)}\t"
                f"{stats.get('zero', 0)}\t{stats['decided']}\t{stats['wins']}\t"
                f"{wr_s}\t{stats['pnl_sum']}"
            )
        print(
            "Same recorded paths, no live orders. Size: rerun with --budget 15. "
            "Spread: add --max-spread 0.05. Grid: --grid."
        )
        return 0

    rows = evaluate_rule(
        markets,
        ask_min=args.ask_min,
        ask_max=args.ask_max,
        ttm_min=args.ttm_min,
        ttm_max=args.ttm_max,
        budget=args.budget,
        max_spread=args.max_spread,
    )
    _print_summary(summarize(rows))
    if args.csv:
        export_hits_csv(rows, args.csv)
        print(f"wrote {len(rows)} rows → {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
