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

from buy.paper_replay import (
    LIVE_FIVE,
    evaluate_market,
    fak_fill,
    fifteen_hedge_specs,
    first_92_entry,
    five_hedge_specs,
    informed_five_specs,
    path_after_entry,
    print_size_near,
    salvage_breakeven,
    summarize,
    ticks_from_trades,
    walk_15m_held,
    walk_5m_held,
)
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


def score_pathlog(markets, series_key: str, budget: float) -> List[dict]:
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
                budget=budget,
            )
        )
    return rows


def score_public(
    http: requests.Session,
    markets: Sequence[dict],
    series_key: str,
    cache_dir: Path,
    workers: int,
    budget: float,
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
            budget=budget,
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


def market_tape(
    http: requests.Session,
    market: dict,
    series_key: str,
    cache_dir: Path,
) -> Tuple[List[dict], List[dict]]:
    start = int(market["start_ts"])
    end = int(market["end_ts"])
    fetch_start = start if series_key == "5m" else max(start, end - 8 * 60)
    trades = fetch_trades(
        http, market["condition_id"], fetch_start, end + 5, cache_dir,
    )
    ticks = ticks_from_trades(trades, fetch_start, end)
    return trades, ticks


def collect_entries(
    http: requests.Session,
    markets: Sequence[dict],
    series_key: str,
    cache_dir: Path,
    workers: int,
    budget: float,
) -> List[dict]:
    """One 92¢ hit (or miss) per market, with ticks kept for hedge variants."""
    ttm_max = SERIES[series_key]["ttm_max"]
    rows: List[Optional[dict]] = [None] * len(markets)

    def one(idx_m: Tuple[int, dict]) -> Tuple[int, dict]:
        idx, market = idx_m
        trades, ticks = market_tape(http, market, series_key, cache_dir)
        hit = first_92_entry(ticks, ttm_max=ttm_max)
        winner = market.get("winner")
        packed: Dict[str, Any] = {
            "slug": market["slug"],
            "series": series_key,
            "winner": winner,
            "ticks": ticks,
            "trades": trades,
            "hit": hit,
            "print_1s": 0.0,
            "print_3s": 0.0,
        }
        if hit is None:
            packed["fill"] = None
            return idx, packed
        packed["print_1s"] = print_size_near(
            trades, float(hit["ts"]), hit["leg"], window_s=0.0,
        )
        packed["print_3s"] = print_size_near(
            trades, float(hit["ts"]), hit["leg"], window_s=1.0,
        )
        packed["fill"] = fak_fill(
            series_key, float(hit["ask"]), None, budget=budget,
        )
        return idx, packed

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, (i, m)) for i, m in enumerate(markets)]
        for fut in as_completed(futs):
            idx, packed = fut.result()
            rows[idx] = packed
            done += 1
            if done % 50 == 0 or done == len(markets):
                print(f"  {series_key} entries {done}/{len(markets)}", flush=True)
    return [r for r in rows if r is not None]


def replay_entries(entries: Sequence[dict], spec, series_key: str) -> List[dict]:
    walker = walk_5m_held if spec.style == "5m" else walk_15m_held
    out = []
    for item in entries:
        hit = item.get("hit")
        fill = item.get("fill")
        winner = item.get("winner")
        if hit is None or fill is None or fill.get("status") == "zero":
            continue
        settled = walker(item["ticks"], hit, fill, winner, spec=spec)
        out.append(
            {
                "slug": item["slug"],
                "series": series_key,
                "hit": True,
                "winner": winner,
                "leg": hit.get("leg"),
                "ask": hit.get("ask"),
                "ttm": hit.get("ttm"),
                "fill": fill.get("status"),
                "shares": fill.get("shares"),
                "notional": fill.get("notional"),
                "avg": fill.get("avg"),
                "won": settled.get("won"),
                "pnl": settled.get("pnl"),
                "exit": settled.get("exit"),
                "exit_reason": settled.get("exit_reason"),
                "exit_bid": settled.get("exit_bid"),
                "exit_ttm": settled.get("exit_ttm"),
                "hedge_late": settled.get("hedge_late"),
                "winner_dump": settled.get("winner_dump"),
                "tick_n": len(item.get("ticks") or []),
                "hedge": spec.name,
            }
        )
    return out


def print_hedge_table(title: str, entries: Sequence[dict], specs, series_key: str) -> List[dict]:
    filled = [
        e for e in entries
        if e.get("hit") is not None and (e.get("fill") or {}).get("status") in ("full", "partial")
    ]
    if not filled:
        print(f"\n=== {title} ===\nno fills")
        return []
    sample = filled[0]["fill"]
    shares = float(sample["shares"])
    notional = float(sample["notional"])
    win = shares - notional
    print()
    print(f"=== {title} ===")
    print(
        f"fills={len(filled)}  clip={shares:.2f} sh / ${notional:.2f}  "
        f"win=+{win:.2f}  wipe=-{notional:.2f}  ride BE={salvage_breakeven(shares, notional, 0.0):.1%}"
    )
    print(
        "perfect-hedge BE (losers only, no false sells):  "
        + "  ".join(
            f"@{c}¢={salvage_breakeven(shares, notional, c / 100.0):.1%}"
            for c in (40, 50, 58, 60, 65, 70)
        )
    )
    header = (
        f"{'spec':<24} {'pnl':>8} {'$/fill':>7} {'WR':>6} {'false':>5} "
        f"{'hedge':>5} {'dump':>5} {'redeem':>6} {'med_exit':>8}"
    )
    print(header)
    print("-" * len(header))
    table = []
    for spec in specs:
        rows = replay_entries(entries, spec, series_key)
        stats = summarize(rows)
        with_pnl = [r for r in rows if r.get("pnl") is not None]
        pnl = sum(float(r["pnl"]) for r in with_pnl)
        wr = stats.get("resolution_win_rate")
        wr_s = f"{100 * wr:.1f}" if wr is not None else "n/a"
        exits = [float(r["exit_bid"]) for r in rows if r.get("exit_bid") is not None]
        exits.sort()
        med = exits[len(exits) // 2] if exits else None
        med_s = f"{med:.2f}" if med is not None else "—"
        line = (
            f"{spec.name:<24} {pnl:8.2f} {stats['pnl_per_hit'] or 0:7.3f} {wr_s:>6} "
            f"{stats['winner_dumps']:5d} {stats['hedges']:5d} {stats['dumps']:5d} "
            f"{stats['redeem_wins']:6d} {med_s:>8}"
        )
        print(line)
        table.append(
            {
                "series": series_key,
                "spec": spec.name,
                "pnl": round(pnl, 4),
                "fills": stats["fills"],
                "winner_dumps": stats["winner_dumps"],
                "hedges": stats["hedges"],
                "dumps": stats["dumps"],
                "redeem_wins": stats["redeem_wins"],
                "redeem_losses": stats["redeem_losses"],
                "median_exit": med,
                "resolution_wr": wr,
            }
        )
    return table


def print_liquidity(title: str, entries: Sequence[dict], budget: float, series_key: str) -> None:
    hits = [e for e in entries if e.get("hit") is not None]
    if not hits:
        return
    fill = fak_fill(series_key, 0.92, None, budget=budget)
    need = float(fill["shares"])
    s1 = sorted(float(e["print_1s"]) for e in hits)
    s3 = sorted(float(e["print_3s"]) for e in hits)

    def pct(arr, p):
        if not arr:
            return 0.0
        return arr[int(p / 100 * (len(arr) - 1))]

    def cover(arr, sh):
        return sum(1 for x in arr if x + 1e-12 >= sh) / len(arr) if arr else 0.0

    print()
    print(f"=== {title} liquidity (last-trade size at the 92¢ fire, NOT restable TOB) ===")
    print(f"need {need:.2f} sh for ${budget:g} @ 92¢ (${fill['notional']:.2f})")
    print(
        f"same-second print p10/p50/p90={pct(s1,10):.2f}/{pct(s1,50):.2f}/{pct(s1,90):.2f}  "
        f"cover {cover(s1, need):.1%}"
    )
    print(
        f"±1s print        p10/p50/p90={pct(s3,10):.2f}/{pct(s3,50):.2f}/{pct(s3,90):.2f}  "
        f"cover {cover(s3, need):.1%}"
    )
    for other in (10.0, 20.0, 25.0):
        o = fak_fill(series_key, 0.92, None, budget=other)
        print(
            f"  ${other:g} needs {o['shares']:.2f} sh (${o['notional']:.2f})  "
            f"same-sec cover {cover(s1, o['shares']):.1%}  ±1s {cover(s3, o['shares']):.1%}"
        )


def size_from_print_pnl(entries: Sequence[dict], spec, series_key: str, budget: float) -> dict:
    """Pessimistic: cap FAK shares at same-second 92¢ print size."""
    walker = walk_5m_held if spec.style == "5m" else walk_15m_held
    rows = []
    for item in entries:
        hit = item.get("hit")
        if hit is None:
            continue
        cap = float(item.get("print_1s") or 0.0)
        fill = fak_fill(series_key, float(hit["ask"]), cap if cap > 0 else 0.0, budget=budget)
        if fill.get("status") == "zero":
            continue
        settled = walker(item["ticks"], hit, fill, item.get("winner"), spec=spec)
        rows.append(
            {
                "hit": True,
                "fill": fill["status"],
                "shares": fill["shares"],
                "notional": fill["notional"],
                "winner": item.get("winner"),
                "leg": hit.get("leg"),
                "pnl": settled.get("pnl"),
                "exit": settled.get("exit"),
                "winner_dump": settled.get("winner_dump"),
                "hedge_late": settled.get("hedge_late"),
            }
        )
    return summarize(rows)


def print_autopsy(title: str, entries: Sequence[dict], spec, series_key: str) -> List[dict]:
    """Bucket live exits and print watcher features on false vs true sells."""
    walker = walk_5m_held if spec.style == "5m" else walk_15m_held
    rows = []
    for item in entries:
        hit = item.get("hit")
        fill = item.get("fill")
        if hit is None or fill is None or fill.get("status") == "zero":
            continue
        held = hit.get("leg")
        winner = item.get("winner")
        settled = walker(item["ticks"], hit, fill, winner, spec=spec)
        feats = path_after_entry(item["ticks"], hit, held)
        won = winner == held
        sold = settled.get("exit") in ("hedge", "dump", "flatten")
        if sold and won:
            bucket = "false_sell"
        elif sold and not won:
            bucket = "true_sell"
        elif (not sold) and won:
            bucket = "winner_ride"
        else:
            bucket = "loser_ride"
        rows.append(
            {
                "slug": item.get("slug"),
                "bucket": bucket,
                "exit": settled.get("exit"),
                "exit_bid": settled.get("exit_bid"),
                "exit_ttm": settled.get("exit_ttm"),
                "hedge_late": settled.get("hedge_late"),
                "ttm": hit.get("ttm"),
                "won": won,
                **feats,
            }
        )
    print()
    print(f"=== {title} autopsy ({spec.name}) ===")
    counts = {}
    for row in rows:
        counts[row["bucket"]] = counts.get(row["bucket"], 0) + 1
    print("  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    def _med(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        vals = sorted(vals)
        return vals[len(vals) // 2]

    for bucket in ("false_sell", "true_sell", "loser_ride", "winner_ride"):
        chunk = [r for r in rows if r["bucket"] == bucket]
        if not chunk:
            continue
        rec = sum(1 for r in chunk if r.get("recovered_70_after_52"))
        print(
            f"  {bucket:<12} n={len(chunk):3d}  "
            f"min_bid med={_med([r.get('min_bid') for r in chunk])}  "
            f"end_bid med={_med([r.get('end_bid') for r in chunk])}  "
            f"sec≤52 med={_med([r.get('sec_le52') for r in chunk])}  "
            f"sec≤40 med={_med([r.get('sec_le40') for r in chunk])}  "
            f"recovered_70={rec}/{len(chunk)}  "
            f"exit_bid med={_med([r.get('exit_bid') for r in chunk])}"
        )
    interesting = [r for r in rows if r["bucket"] in ("false_sell", "true_sell", "loser_ride")]
    print("  slug                              bucket       exit  bid   ttm  min   end  recov")
    for row in interesting:
        print(
            f"  {str(row.get('slug') or '')[:32]:<32} {row['bucket']:<12} "
            f"{str(row.get('exit') or ''):<6} "
            f"{'' if row.get('exit_bid') is None else f'{row['exit_bid']:.2f}':>5} "
            f"{'' if row.get('exit_ttm') is None else f'{row['exit_ttm']:.0f}':>4} "
            f"{'' if row.get('min_bid') is None else f'{row['min_bid']:.2f}':>5} "
            f"{'' if row.get('end_bid') is None else f'{row['end_bid']:.2f}':>5} "
            f"{'Y' if row.get('recovered_70_after_52') else 'n'}"
        )
    return rows


def live_book_snapshot(http: requests.Session) -> None:
    """Public CLOB TOB for the current 5m/15m clocks — size at 92¢ now, not history."""
    print("\n=== live CLOB snapshot (now, not the week tape) ===")
    for series_key, slug in (
        ("5m", "btc-up-or-down-5m"),
        ("15m", "btc-up-or-down-15m"),
    ):
        try:
            resp = http.get(
                f"{GAMMA}/events",
                params={"slug": slug, "closed": "false", "limit": 1},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json() or []
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"  {series_key}: gamma fail {exc}")
            continue
        if not events:
            print(f"  {series_key}: no open event")
            continue
        markets = events[0].get("markets") or []
        if not markets:
            print(f"  {series_key}: no markets on event")
            continue
        m = markets[0]
        tokens = m.get("clobTokenIds") or m.get("clob_token_ids")
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except (TypeError, ValueError):
                tokens = []
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except (TypeError, ValueError):
                outcomes = []
        print(f"  {series_key} {events[0].get('slug')}")
        for outcome, token in zip(outcomes or [], tokens or []):
            try:
                book = http.get(
                    "https://clob.polymarket.com/book",
                    params={"token_id": token},
                    timeout=15,
                )
                book.raise_for_status()
                raw = book.json() or {}
            except (requests.RequestException, ValueError, TypeError) as exc:
                print(f"    {outcome}: book fail {exc}")
                continue
            asks = raw.get("asks") or []
            bids = raw.get("bids") or []

            def _lvl(row):
                if isinstance(row, dict):
                    try:
                        return float(row.get("price")), float(row.get("size") or 0)
                    except (TypeError, ValueError):
                        return None, 0.0
                try:
                    return float(row[0]), float(row[1])
                except (TypeError, ValueError, IndexError):
                    return None, 0.0

            parsed_asks = [(_lvl(r)) for r in asks]
            parsed_bids = [(_lvl(r)) for r in bids]
            parsed_asks = [(p, s) for p, s in parsed_asks if p is not None]
            parsed_bids = [(p, s) for p, s in parsed_bids if p is not None]
            ask_px = ask_sz = bid_px = bid_sz = None
            if parsed_asks:
                ask_px, ask_sz = min(parsed_asks, key=lambda x: x[0])
            if parsed_bids:
                bid_px, bid_sz = max(parsed_bids, key=lambda x: x[0])
            near92 = sum(sz for px, sz in parsed_asks if px <= 0.92 + 1e-12)
            print(
                f"    {outcome}: bid {bid_px} x {bid_sz}  ask {ask_px} x {ask_sz}  "
                f"ask≤92 size={near92:.2f}"
            )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="92¢ last-week paper P&L (1s ticks, live hedge gates)")
    ap.add_argument("--hours", type=float, default=168.0, help="lookback hours (default 168 = 7d)")
    ap.add_argument("--series", default="both", help="5m | 15m | both")
    ap.add_argument("--dir", type=Path, default=None, help="pathlog/ticks if present")
    ap.add_argument("--cache", type=Path, default=Path("/tmp/poly-92c-week"))
    ap.add_argument("--csv", type=Path, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--end-ts", type=float, default=None)
    ap.add_argument(
        "--budget",
        type=float,
        default=10.0,
        help="USDC per fill (default 10). Live $2.50 rails are not used.",
    )
    ap.add_argument(
        "--hedge-sweep",
        action="store_true",
        help="Replay the same 92¢ fills through ride / persist / dump / last-minute 15m 60¢.",
    )
    ap.add_argument(
        "--size-from-print",
        action="store_true",
        help="Also score a pessimistic fill cap at same-second 92¢ last-trade size.",
    )
    ap.add_argument(
        "--hedge-autopsy",
        action="store_true",
        help="Bucket false vs true sells and score informed (watcher) hedge rules.",
    )
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
        + f"  budget=${args.budget:g}"
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
    sweep_tables: List[dict] = []
    for series_key in want:
        ttm = SERIES[series_key]["ttm_max"]
        tape = "last-trade-1s"
        pathlog = []
        if tick_dir is not None:
            pathlog = load_pathlog_markets(tick_dir, series_key, start_ts, end_ts)
        if pathlog and not args.hedge_sweep and not args.hedge_autopsy:
            print(f"\n{series_key}: {len(pathlog)} pathlog markets (TOB 1s)")
            rows = score_pathlog(pathlog, series_key, args.budget)
            tape = "pathlog-tob-1s"
            stats = summarize(rows)
            print_stats(f"{series_key} buy@92¢ last {ttm:g}s", stats, tape)
            all_rows.extend(rows)
            continue
        print(f"\n{series_key}: fetching Gamma calendar + last-trades (public, no keys)")
        markets = enumerate_gamma(http, series_key, start_ts, end_ts)
        print(f"  gamma markets ending in window: {len(markets)}")
        if not markets:
            print_stats(f"{series_key} 92¢ last {ttm:g}s", summarize([]), tape)
            continue
        if args.hedge_sweep or args.hedge_autopsy:
            entries = collect_entries(
                http, markets, series_key, args.cache, args.workers, args.budget,
            )
            if args.hedge_autopsy:
                live = LIVE_FIVE if series_key == "5m" else fifteen_hedge_specs()[1]
                print_autopsy(
                    f"{series_key} 92¢ last {ttm:g}s", entries, live, series_key,
                )
                specs = informed_five_specs() if series_key == "5m" else fifteen_hedge_specs()
                sweep_tables.extend(
                    print_hedge_table(
                        f"{series_key} informed hedge @ ${args.budget:g}",
                        entries, specs, series_key,
                    )
                )
            if args.hedge_sweep:
                print_liquidity(
                    f"{series_key} 92¢ last {ttm:g}s", entries, args.budget, series_key,
                )
                specs = five_hedge_specs() if series_key == "5m" else fifteen_hedge_specs()
                sweep_tables.extend(
                    print_hedge_table(
                        f"{series_key} hedge sweep @ ${args.budget:g}",
                        entries, specs, series_key,
                    )
                )
                if args.size_from_print:
                    live = specs[1] if len(specs) > 1 else specs[0]
                    ride = specs[0]
                    print(f"\n=== {series_key} pessimistic same-second print cap ===")
                    for spec in (ride, live):
                        st = size_from_print_pnl(entries, spec, series_key, args.budget)
                        print(
                            f"  {spec.name}: fills={st['fills']} partial={st['partial']} "
                            f"zero skipped  pnl={st['pnl_sum']} spend={st['spend']} "
                            f"false={st['winner_dumps']}"
                        )
            continue
        rows = score_public(
            http, markets, series_key, args.cache, args.workers, args.budget,
        )
        stats = summarize(rows)
        print_stats(f"{series_key} buy@92¢ last {ttm:g}s", stats, tape)
        all_rows.extend(rows)

    if args.hedge_sweep or args.hedge_autopsy:
        if args.hedge_sweep:
            live_book_snapshot(http)
        if args.csv:
            args.csv.parent.mkdir(parents=True, exist_ok=True)
            fields = [
                "series", "spec", "pnl", "fills", "winner_dumps", "hedges",
                "dumps", "redeem_wins", "redeem_losses", "median_exit",
                "resolution_wr",
            ]
            with args.csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for row in sweep_tables:
                    writer.writerow(row)
            print(f"\nwrote {len(sweep_tables)} rows → {args.csv}")
        return 0
    if args.csv:
        export_csv(all_rows, args.csv)
        print(f"\nwrote {len(all_rows)} rows → {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
