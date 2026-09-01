#!/usr/bin/env python3
"""Last-week paper P&L for 92¢ entries on 5m (last 60s) and 15m (last 3 min).

Replays live entry gates + live hedge gates on a 1-second tape reconstructed
from public Data API last-trades (pathlog TOB is used when present).

  python check_92c_week_backtest.py
  python check_92c_week_backtest.py --hours 168 --series 5m
  python check_92c_week_backtest.py --hours 168 --dir pathlog/ticks

No orders. No .env. Does not start systemd bots.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from buy.paper_replay import evaluate_market, summarize, ticks_from_trades
from check_path_backtest import iter_markets, matches_series

GAMMA = "https://gamma-api.polymarket.com"
TRADES_URL = "https://data-api.polymarket.com/trades"
USER_AGENT = "poly-money-maker-92c-week-backtest/1.0"

SERIES = {
    "5m": {
        "series_slug": "btc-up-or-down-5m",
        "duration_s": 300,
        "ttm_max": 60.0,
        "step_s": 300,
        "slug_prefix": "btc-updown-5m-",
        "label": "5m",
    },
    "15m": {
        "series_slug": "btc-up-or-down-15m",
        "duration_s": 900,
        "ttm_max": 180.0,
        "step_s": 900,
        "slug_prefix": "btc-updown-15m-",
        "label": "15m",
    },
}


def session() -> requests.Session:
    out = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    out.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    out.headers["User-Agent"] = USER_AGENT
    return out


def _parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _end_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def winner_from_market(raw: dict) -> Optional[str]:
    outcomes = _parse_json_field(raw.get("outcomes")) or []
    prices = _parse_json_field(raw.get("outcomePrices")) or []
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    ranked = sorted(
        zip([str(x).lower() for x in outcomes], [float(x) for x in prices]),
        key=lambda item: item[1],
        reverse=True,
    )
    if ranked and ranked[0][1] >= 0.99:
        return ranked[0][0]
    return None


def enumerate_gamma(
    http: requests.Session,
    series_key: str,
    start_ts: float,
    end_ts: float,
) -> List[dict]:
    cfg = SERIES[series_key]
    out: Dict[str, dict] = {}
    for closed in ("true", "false"):
        offset = 0
        for _ in range(80):
            resp = http.get(
                f"{GAMMA}/events",
                params={
                    "series_slug": cfg["series_slug"],
                    "closed": closed,
                    "limit": 50,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "false",
                },
                timeout=25,
            )
            if resp.status_code != 200:
                break
            events = resp.json() or []
            if not events:
                break
            oldest = None
            for event in events:
                for raw in event.get("markets") or []:
                    slug = str(raw.get("slug") or event.get("slug") or "")
                    end = _end_ts(raw.get("endDate") or event.get("endDate"))
                    if end is None:
                        continue
                    oldest = end if oldest is None else min(oldest, end)
                    if end < start_ts or end > end_ts + 1:
                        continue
                    cid = str(raw.get("conditionId") or raw.get("condition_id") or "")
                    if not cid:
                        continue
                    m_ts = None
                    if "-" in slug:
                        tail = slug.rsplit("-", 1)[-1]
                        if tail.isdigit() and int(tail) > 1_700_000_000:
                            m_ts = int(tail)
                    start = float(m_ts) if m_ts else end - cfg["duration_s"]
                    out[cid] = {
                        "slug": slug or f"{cfg['slug_prefix']}{int(start)}",
                        "condition_id": cid,
                        "start_ts": start,
                        "end_ts": end,
                        "winner": winner_from_market(raw),
                        "closed": bool(raw.get("closed")),
                        "series": series_key,
                    }
            offset += len(events)
            if oldest is not None and oldest < start_ts:
                break
            if len(events) < 50:
                break
    return sorted(out.values(), key=lambda m: m["end_ts"])


def fetch_trades(
    http: requests.Session,
    condition: str,
    start: int,
    end: int,
    cache_dir: Path,
) -> List[dict]:
    cache = cache_dir / f"{condition}_{start}_{end}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    rows: List[dict] = []
    offset = 0
    while offset <= 20_000:
        resp = http.get(
            TRADES_URL,
            params={
                "market": condition,
                "start": start,
                "end": end,
                "limit": 1000,
                "offset": offset,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            break
        batch = resp.json() or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
        time.sleep(0.02)
    slim = [
        {
            "ts": int(float(r["timestamp"])),
            "px": float(r["price"]),
            "size": float(r.get("size") or 0),
            "outcome": str(r.get("outcome") or "").lower(),
        }
        for r in rows
        if r.get("timestamp") is not None and r.get("price") is not None
    ]
    slim.sort(key=lambda item: item["ts"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(slim))
    return slim


def load_pathlog_markets(tick_dir: Path, series_key: str, start_ts: float, end_ts: float):
    out = []
    for market in iter_markets(tick_dir):
        if not matches_series(market.series, market.slug, series_key):
            continue
        if market.end_ts < start_ts or market.end_ts > end_ts + 1:
            continue
        out.append(market)
    return out


def score_pathlog(markets, series_key: str) -> List[dict]:
    ttm_max = SERIES[series_key]["ttm_max"]
    rows = []
    for market in markets:
        # Prefer Gamma winner already stamped on the JSONL.
        ticks = []
        for row in market.ticks:
            if "ult" not in row:
                row = dict(row)
                row["ult"] = row.get("ua")
                row["dlt"] = row.get("da")
            ticks.append(row)
        rows.append(
            evaluate_market(
                ticks,
                series=series_key,
                ttm_max=ttm_max,
                winner=market.winner,
                slug=market.slug,
            )
        )
    return rows


def score_public(
    http: requests.Session,
    markets: Sequence[dict],
    series_key: str,
    cache_dir: Path,
    workers: int,
) -> List[dict]:
    ttm_max = SERIES[series_key]["ttm_max"]
    duration = SERIES[series_key]["duration_s"]
    rows: List[Optional[dict]] = [None] * len(markets)

    def one(idx_m: Tuple[int, dict]) -> Tuple[int, dict]:
        idx, market = idx_m
        start = int(market["start_ts"])
        end = int(market["end_ts"])
        # Whole market for 5m; last 8 minutes of 15m (pathlog window) so a
        # T-180 entry still has a hedge path.
        fetch_start = start if series_key == "5m" else max(start, end - 8 * 60)
        trades = fetch_trades(
            http, market["condition_id"], fetch_start, end + 5, cache_dir,
        )
        ticks = ticks_from_trades(trades, fetch_start, end)
        scored = evaluate_market(
            ticks,
            series=series_key,
            ttm_max=ttm_max,
            winner=market.get("winner"),
            slug=market["slug"],
        )
        scored["trade_n"] = len(trades)
        scored["duration_s"] = duration
        return idx, scored

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, (i, m)) for i, m in enumerate(markets)]
        for fut in as_completed(futs):
            idx, scored = fut.result()
            rows[idx] = scored
            done += 1
            if done % 50 == 0 or done == len(markets):
                print(f"  {series_key} scored {done}/{len(markets)}", flush=True)
    return [r for r in rows if r is not None]


def print_stats(title: str, stats: dict, tape: str) -> None:
    wr = stats.get("redeem_win_rate")
    wr_s = f"{wr:.1%}" if wr is not None else "n/a"
    res = stats.get("resolution_win_rate")
    res_s = f"{res:.1%}" if res is not None else "n/a"
    print()
    print(f"=== {title} ===")
    print(f"tape={tape}")
    print(
        f"markets={stats['markets']}  hits={stats['hits']}  fills={stats['fills']}  "
        f"full={stats['full']}  partial={stats['partial']}  zero={stats['zero']}  "
        f"misses={stats['misses']}"
    )
    print(
        f"pnl={stats['pnl_sum']}  spend={stats['spend']}  "
        f"pnl/fill={stats['pnl_per_hit']}  redeem_win_rate={wr_s}  "
        f"resolution_win_rate={res_s}"
    )
    print(
        f"redeem_wins={stats['redeem_wins']}  redeem_losses={stats['redeem_losses']}  "
        f"hedges={stats['hedges']}  dumps={stats['dumps']}  flattens={stats['flattens']}  "
        f"winner_dumps={stats['winner_dumps']}  hedge_late={stats['hedge_late']}  "
        f"unresolved={stats['unresolved']}  mean_ticks={stats['mean_ticks']}"
    )


def export_csv(rows: Sequence[dict], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "slug", "series", "hit", "winner", "leg", "ask", "ttm", "fill",
        "shares", "notional", "avg", "won", "pnl", "exit", "exit_reason",
        "exit_bid", "exit_ttm", "hedge_late", "winner_dump", "tick_n",
    ]
    with dest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="92¢ last-week paper P&L (1s ticks, live hedge gates)")
    ap.add_argument("--hours", type=float, default=168.0, help="lookback hours (default 168 = 7d)")
    ap.add_argument("--series", default="both", help="5m | 15m | both")
    ap.add_argument("--dir", type=Path, default=None, help="pathlog/ticks if present")
    ap.add_argument("--cache", type=Path, default=Path("/tmp/poly-92c-week"))
    ap.add_argument("--csv", type=Path, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--end-ts", type=float, default=None)
    args = ap.parse_args(argv)

    end_ts = float(args.end_ts) if args.end_ts else time.time()
    start_ts = end_ts - float(args.hours) * 3600.0
    want = []
    if args.series in ("both", "5m", "5"):
        want.append("5m")
    if args.series in ("both", "15m", "15"):
        want.append("15m")
    if not want:
        print("series must be 5m, 15m, or both", file=sys.stderr)
        return 2

    print(
        "92¢ paper week  "
        + datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        + " → "
        + datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        + " UTC"
    )
    print(
        "5m: last 60s @ 92¢, FAK 92¢, live hedge "
        "(persist 1s @ 50/52 dump 40 flatten <75 last-30s 58/60/62)."
    )
    print(
        "15m: last 180s @ 92¢, FAK at quoted ask, 15m hedge "
        "(35/40 + inverted 70/30 GUI, toxic dump ≤35)."
    )
    print(
        "Not replayed: BTC/PTB oracle, empty FAK, POST RTT. "
        "1s tape = pathlog TOB if --dir, else last-trade reconstruction."
    )

    tick_dir = args.dir
    if tick_dir is None:
        default = Path("pathlog") / "ticks"
        tick_dir = default if default.exists() and any(default.glob("*.jsonl")) else None

    http = session()
    all_rows: List[dict] = []
    for series_key in want:
        ttm = SERIES[series_key]["ttm_max"]
        tape = "last-trade-1s"
        pathlog = []
        if tick_dir is not None:
            pathlog = load_pathlog_markets(tick_dir, series_key, start_ts, end_ts)
        if pathlog:
            print(f"\n{series_key}: {len(pathlog)} pathlog markets (TOB 1s)")
            rows = score_pathlog(pathlog, series_key)
            tape = "pathlog-tob-1s"
        else:
            print(f"\n{series_key}: fetching Gamma calendar + last-trades (public, no keys)")
            markets = enumerate_gamma(http, series_key, start_ts, end_ts)
            print(f"  gamma markets ending in window: {len(markets)}")
            if not markets:
                print_stats(f"{series_key} 92¢ last {ttm:g}s", summarize([]), tape)
                continue
            rows = score_public(http, markets, series_key, args.cache, args.workers)
        stats = summarize(rows)
        print_stats(f"{series_key} buy@92¢ last {ttm:g}s", stats, tape)
        all_rows.extend(rows)

    if args.csv:
        export_csv(all_rows, args.csv)
        print(f"\nwrote {len(all_rows)} rows → {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
