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
    ap.add_argument("--csv", type=Path, default=None, help="write per-market hit rows")
    ap.add_argument("--export-market", default="", help="slug to dump as tick CSV")
    ap.add_argument("--grid", action="store_true", help="print ask × ttm_max win-rate table")
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
