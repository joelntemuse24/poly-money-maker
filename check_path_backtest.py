#!/usr/bin/env python3
"""Query recorded market paths: what if we entered at price X with Y time left?

Reads pathlog/ticks/*.jsonl written by pathlog.py. Does not place orders.

Examples (on the VM):
  python check_path_backtest.py --ask-min 0.80 --ask-max 0.85 --ttm-max 120
  python check_path_backtest.py --grid --budget 3
  python check_path_backtest.py --export-market btc-updown-5m-1786528500 --csv /tmp/m.csv
  python check_path_backtest.py --ask-min 0.98 --ask-max 0.99 --ttm-max 90 --csv /tmp/hits.csv
"""

from __future__ import annotations

import argparse
import csv
import json
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
    """
    for row in ticks:
        ttm = _f(row.get("ttm"))
        if ttm is None or not (ttm_min <= ttm <= ttm_max):
            continue
        candidates: List[Tuple[str, float, Optional[float]]] = []
        for leg, ask_key, bid_key in (("up", "ua", "ub"), ("down", "da", "db")):
            ask = _f(row.get(ask_key))
            if ask is None or not (ask_min - 1e-12 <= ask <= ask_max + 1e-12):
                continue
            bid = _f(row.get(bid_key))
            if max_spread is not None and bid is not None and (ask - bid) > max_spread + 1e-12:
                continue
            if max_spread is not None and bid is None:
                continue
            candidates.append((leg, ask, bid))
        if not candidates:
            continue
        leg, ask, bid = max(candidates, key=lambda item: item[1])
        return {
            "ts": row.get("ts"),
            "ttm": ttm,
            "leg": leg,
            "ask": ask,
            "bid": bid,
        }
    return None


def hypothetical_pnl(ask: float, won: bool, budget: float) -> float:
    if ask <= 0 or budget <= 0:
        return 0.0
    shares = budget / ask
    if won:
        return shares * 1.0 - budget
    return -budget


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
                    "ttm": None,
                    "won": None,
                    "pnl": None,
                }
            )
            continue
        won = winner is not None and hit["leg"] == winner
        pnl = hypothetical_pnl(float(hit["ask"]), bool(won), budget) if winner else None
        rows.append(
            {
                "slug": market.slug,
                "series": market.series,
                "hit": True,
                "winner": winner,
                "leg": hit["leg"],
                "ask": hit["ask"],
                "ttm": hit["ttm"],
                "won": won if winner else None,
                "pnl": None if pnl is None else round(pnl, 4),
            }
        )
    return rows


def summarize(rows: Sequence[dict]) -> dict:
    hits = [r for r in rows if r.get("hit")]
    decided = [r for r in hits if r.get("won") is not None]
    wins = [r for r in decided if r.get("won")]
    pnl = [float(r["pnl"]) for r in decided if r.get("pnl") is not None]
    return {
        "markets": len(rows),
        "hits": len(hits),
        "decided": len(decided),
        "wins": len(wins),
        "win_rate": (len(wins) / len(decided)) if decided else None,
        "pnl_sum": round(sum(pnl), 4) if pnl else None,
        "misses": len(rows) - len(hits),
        "unresolved": len(hits) - len(decided),
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
        writer.writerow(["ts", "ttm_s", "up_bid", "up_ask", "dn_bid", "dn_ask"])
        for row in market.ticks:
            writer.writerow(
                [
                    row.get("ts"),
                    row.get("ttm"),
                    row.get("ub"),
                    row.get("ua"),
                    row.get("db"),
                    row.get("da"),
                ]
            )


def export_hits_csv(rows: Sequence[dict], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fields = ["slug", "series", "hit", "leg", "ask", "ttm", "winner", "won", "pnl"]
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
        f"decided={stats['decided']}  wins={stats['wins']}  "
        f"win_rate={wr_s}  pnl={stats['pnl_sum']}  "
        f"misses={stats['misses']}  unresolved={stats['unresolved']}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest entry rules against pathlog ticks")
    ap.add_argument("--dir", type=Path, default=TICK_DIR)
    ap.add_argument("--ask-min", type=float, default=0.80)
    ap.add_argument("--ask-max", type=float, default=0.99)
    ap.add_argument("--ttm-min", type=float, default=0.0, help="seconds left, inclusive")
    ap.add_argument("--ttm-max", type=float, default=120.0, help="seconds left, inclusive")
    ap.add_argument("--budget", type=float, default=3.0)
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
        print("ask\tttm_max\thits\tdecided\twins\twin_rate\tpnl")
        for row in table:
            wr = row["win_rate"]
            wr_s = f"{wr:.3f}" if wr is not None else ""
            print(
                f"{row['ask']:.2f}\t{row['ttm_max']}\t{row['hits']}\t"
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
