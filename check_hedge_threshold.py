#!/usr/bin/env python3
"""Score earlier 5m hedge triggers. No orders. No .env.

Pathlog TOB (best):

    python check_path_backtest.py --hedge-sweep --series 5m --budget 2.5

Public last-trade (this file) when ticks are not on this box:

    python check_hedge_threshold.py --csv history.csv --hours 15

Last-trade is not a restable bid. Empty FAKs, GUI, and POST latency are
not replayed. Use pathlog --hedge-sweep on the VM for the book model.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from check_path_backtest import (
    TICK_DIR,
    evaluate_rule,
    hedge_sweep_variants,
    iter_markets,
    matches_series,
    paper_kwargs_from,
    summarize,
    template_from_strategy,
)

ET = ZoneInfo("America/New_York")
GAMMA_EVENT = "https://gamma-api.polymarket.com/events"
TRADES_URL = "https://data-api.polymarket.com/trades"
USER_AGENT = "Mozilla/5.0 (compatible; poly-money-maker-hedge-research/1.0)"
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
RANGE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s+"
    r"(\d{1,2}):(\d{2})\s*([AP]M)\s*-\s*(\d{1,2}):(\d{2})\s*([AP]M)",
    re.I,
)
# Data API / UI sometimes drop the first AM/PM or use an en-dash:
# "August 27, 5:00-5:05PM ET" / "August 27, 5:00AM–5:05AM ET".
RANGE_RE_LOOSE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s+"
    r"(\d{1,2}):(\d{2})\s*([AP]M)?\s*[-–—]\s*(\d{1,2}):(\d{2})\s*([AP]M)",
    re.I,
)
HOUR_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s+(\d{1,2})\s*([AP]M)\s*ET",
    re.I,
)
# check_fetch_trades writes slug=btc-updown-5m-{start}. Title is often just
# "BTC Up or Down 5m" (no clock) — RANGE_RE then misses every 5m row.
SLUG_5M_RE = re.compile(r"btc-updown-5m-(\d{10,})", re.I)
SLUG_15M_RE = re.compile(r"btc-updown-15m-(\d{10,})", re.I)
THRESHOLDS = (0.75, 0.70, 0.65, 0.60, 0.55, 0.53)
PERSIST_S = (2.0, 5.0)
DROPS = (0.05, 0.08, 0.10)


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


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _et_hm(hour: int, minute: int, ap: str) -> tuple[int, int]:
    hour = int(hour) % 12
    if ap.upper() == "PM":
        hour += 12
    return hour, int(minute)


def five_m_slug_start(text: str) -> Optional[int]:
    """Start unix from ``btc-updown-5m-{start}`` (Gamma / Data API slug)."""
    m = SLUG_5M_RE.search(text or "")
    if not m:
        return None
    ts = int(m.group(1))
    if ts > 1_700_000_000:
        return ts
    return None


def _range_match(name: str):
    return RANGE_RE.search(name or "") or RANGE_RE_LOOSE.search(name or "")


def series_of(name: str) -> str:
    if five_m_slug_start(name):
        return "5m"
    if SLUG_15M_RE.search(name or ""):
        return "15m"
    m = _range_match(name)
    if m:
        h1, mi1, ap1, h2, mi2, ap2 = m.group(3, 4, 5, 6, 7, 8)
        if not ap2:
            return "unknown"
        if not ap1:
            ap1 = ap2

        def mins(h: str, mi: str, ap: str) -> int:
            hh, mm = _et_hm(int(h), int(mi), ap)
            return hh * 60 + mm

        dur = (mins(h2, mi2, ap2) - mins(h1, mi1, ap1)) % (24 * 60)
        if dur == 5:
            return "5m"
        if dur == 15:
            return "15m"
        return f"range_{dur}m"
    if HOUR_RE.search(name or "") or re.search(r",\s*\d{1,2}\s*(AM|PM)\s*ET\s*$", name or "", re.I):
        return "hourly"
    return "unknown"


def five_m_start_ts(name: str, year: int) -> Optional[int]:
    slug_ts = five_m_slug_start(name)
    if slug_ts is not None:
        return slug_ts
    m = _range_match(name)
    if not m:
        return None
    ap1 = m.group(5) or m.group(8)
    if not ap1:
        return None
    month = MONTHS[m.group(1).lower()]
    day = int(m.group(2))
    hour, minute = _et_hm(int(m.group(3)), int(m.group(4)), ap1)
    dt = datetime(year, month, day, hour, minute, tzinfo=ET)
    return int(dt.timestamp())


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rdr = csv.DictReader(handle)
        rdr.fieldnames = [((n or "").strip().strip('"')) for n in (rdr.fieldnames or [])]
        rows: list[dict] = []
        for raw in rdr:
            row = {(k or "").strip().strip('"'): v for k, v in raw.items()}
            ts = _f(row.get("timestamp"))
            if ts is not None and ts > 1e12:
                ts = ts / 1000.0
            usdc = _f(row.get("usdcAmount")) or 0.0
            tok = _f(row.get("tokenAmount")) or 0.0
            if ts is None:
                continue
            slug = (row.get("slug") or row.get("eventSlug") or "").strip()
            title = (row.get("marketName") or row.get("title") or "").strip()
            rows.append(
                {
                    "market": title or slug,
                    "slug": slug,
                    "action": (row.get("action") or row.get("side") or "").title(),
                    "usdc": usdc,
                    "tok": tok,
                    "leg": (row.get("tokenName") or row.get("outcome") or "").strip(),
                    "ts": ts,
                    "px": (usdc / tok) if tok else _f(row.get("price")),
                }
            )
    rows.sort(key=lambda item: item["ts"])
    return rows


def fetch_event(http: requests.Session, slug: str) -> Optional[dict]:
    resp = http.get(GAMMA_EVENT, params={"slug": slug}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return None
    ev = data[0]
    markets = ev.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    outcomes = m.get("outcomes")
    prices = m.get("outcomePrices")
    tokens = m.get("clobTokenIds")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(prices, str):
        prices = json.loads(prices)
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    winner = None
    if outcomes and prices and len(outcomes) == len(prices):
        ranked = sorted(
            zip([str(x).lower() for x in outcomes], [float(x) for x in prices]),
            key=lambda item: item[1],
            reverse=True,
        )
        if ranked and ranked[0][1] >= 0.99:
            winner = ranked[0][0]
    token = {}
    if outcomes and tokens and len(outcomes) == len(tokens):
        token = {str(o).lower(): str(t) for o, t in zip(outcomes, tokens)}
    return {
        "slug": ev.get("slug") or slug,
        "condition": m.get("conditionId"),
        "winner": winner,
        "tokens": token,
        "closed": bool(m.get("closed")),
    }


def fetch_trades(
    http: requests.Session,
    condition: str,
    start: int,
    end: int,
    cache_dir: Path,
) -> list[dict]:
    cache = cache_dir / f"{condition}_{start}_{end}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    rows: list[dict] = []
    offset = 0
    while offset <= 10_000:
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
        resp.raise_for_status()
        batch = resp.json() or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
        time.sleep(0.03)
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


def prints_for(trades: Iterable[dict], leg: str, after_ts: float) -> list[tuple[float, float]]:
    out = []
    for row in trades:
        if row["outcome"] != leg:
            continue
        if row["ts"] <= after_ts + 1e-9:
            continue
        out.append((float(row["ts"]), float(row["px"])))
    return out


def first_touch(
    path: list[tuple[float, float]], threshold: float
) -> Optional[tuple[float, float]]:
    for ts, px in path:
        if px <= threshold + 1e-12:
            return ts, px
    return None


def persist_touch(
    path: list[tuple[float, float]], threshold: float, persist_s: float
) -> Optional[tuple[float, float]]:
    armed: Optional[float] = None
    last: Optional[tuple[float, float]] = None
    for ts, px in path:
        if px <= threshold + 1e-12:
            if armed is None:
                armed = ts
            last = (ts, px)
            if ts - armed >= persist_s - 1e-12:
                return last
        else:
            armed = None
            last = None
    return None


def fade_touch(
    path: list[tuple[float, float]], fill_px: float, drop: float
) -> Optional[tuple[float, float]]:
    return first_touch(path, fill_px - drop)


def paper_exit(notional: float, shares: float, exit_px: Optional[float], won: bool) -> float:
    if shares <= 0 or notional <= 0:
        return 0.0
    if exit_px is None:
        return (shares - notional) if won else -notional
    return shares * exit_px - notional


def money(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:+.2f}"


def wr(wins: int, n: int) -> str:
    if not n:
        return ""
    return f"{wins / n:.1%}"


def group_five(rows: list[dict]) -> dict[str, dict]:
    mk: dict[str, dict] = {}
    for row in rows:
        if row["series"] != "5m":
            continue
        item = mk.setdefault(
            row["market"],
            {"buys": [], "sells": [], "redeems": []},
        )
        if row["action"] == "Buy":
            item["buys"].append(row)
        elif row["action"] == "Sell":
            item["sells"].append(row)
        elif row["action"] == "Redeem":
            item["redeems"].append(row)
    return mk


def run_pathlog_hedge_sweep(tick_dir: Path, budget: float) -> Optional[str]:
    markets = [
        m
        for m in iter_markets(tick_dir)
        if matches_series(m.series, m.slug, "5m")
    ]
    if not markets:
        return None
    tmpl_path = Path(__file__).resolve().parent / "strategy_buy5m.example.json"
    tmpl = template_from_strategy(tmpl_path)
    lines = [
        "PATHLOG --hedge-sweep (late 75–90 / 120s first touch; recorded TOB)",
        "name\tthreshold\tpersist\tdrop\tgui\thits\tdecided\twins\twr\tpnl\t"
        "hedges\twinner_dumps\tloser_hedges",
    ]
    for variant in hedge_sweep_variants(tmpl):
        rows = evaluate_rule(
            markets,
            ask_min=float(variant["ask_min"]),
            ask_max=float(variant["ask_max"]),
            ttm_min=0.0,
            ttm_max=float(variant["ttm_max"]),
            budget=float(variant.get("budget") or budget),
            max_spread=variant.get("max_spread"),
            paper=bool(variant.get("paper")),
            paper_kwargs=paper_kwargs_from(variant),
        )
        stats = summarize(rows)
        wr_s = f"{stats['win_rate']:.3f}" if stats["win_rate"] is not None else ""
        drop = variant.get("hedge_drop_from_fill")
        lines.append(
            f"{variant['name']}\t{float(variant.get('hedge_threshold') or 0):.2f}\t"
            f"{float(variant.get('hedge_persist_s') or 0):g}\t"
            f"{'' if drop is None else f'{float(drop):.2f}'}\t"
            f"{int(bool(variant.get('hedge_require_gui', True)))}\t"
            f"{stats['hits']}\t{stats['decided']}\t{stats['wins']}\t{wr_s}\t"
            f"{stats['pnl_sum']}\t{stats.get('hedges', 0)}\t"
            f"{stats.get('winner_dumps', 0)}\t{stats.get('loser_hedges', 0)}"
        )
    lines.append(
        "winner_dumps = sold a market that still resolved to the held leg."
    )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Paper-score earlier 5m hedges")
    ap.add_argument("--csv", type=Path, help="UI or check_fetch_trades history CSV")
    ap.add_argument("--hours", type=float, default=15.0)
    ap.add_argument("--dir", type=Path, default=TICK_DIR)
    ap.add_argument("--budget", type=float, default=2.5)
    ap.add_argument("--cache", type=Path, default=Path("/tmp/poly_hedge_cache"))
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    lines: list[str] = []

    def p(*parts: Any) -> None:
        lines.append(" ".join(str(x) for x in parts))

    pathlog = run_pathlog_hedge_sweep(args.dir, args.budget)
    if pathlog:
        p(pathlog)
        p()
    else:
        p(
            "No pathlog ticks in",
            args.dir,
            "— scoring public last-trades from --csv instead.",
            "On the VM: .venv/bin/python check_path_backtest.py --hedge-sweep --series 5m",
        )
        p()

    if not args.csv:
        if pathlog:
            text = "\n".join(lines) + "\n"
            print(text, end="")
            if args.out:
                args.out.write_text(text)
            return 0
        print("need --csv or pathlog/ticks", file=sys.stderr)
        return 2

    raw = load_csv(args.csv)
    if not raw:
        print("empty csv", file=sys.stderr)
        return 1
    cut = raw[-1]["ts"] - float(args.hours) * 3600.0
    win = [r for r in raw if r["ts"] >= cut]
    for row in win:
        row["series"] = series_of(row.get("slug") or row["market"])
    p(
        "CSV",
        args.csv.name,
        "window",
        datetime.fromtimestamp(cut, tz=timezone.utc).isoformat(),
        "->",
        datetime.fromtimestamp(raw[-1]["ts"], tz=timezone.utc).isoformat(),
        "UTC",
        "rows",
        len(win),
    )

    markets = group_five(win)
    http = session()
    jobs = []
    for name, bag in markets.items():
        if not bag["buys"]:
            continue
        year = datetime.fromtimestamp(bag["buys"][0]["ts"], tz=ET).year
        start = five_m_start_ts(name, year)
        if start is None:
            continue
        jobs.append((name, bag, start, f"btc-updown-5m-{start}"))

    events: dict[str, Optional[dict]] = {}
    trades: dict[str, list[dict]] = {}

    def load_one(job: tuple) -> tuple[str, Optional[dict], list[dict]]:
        name, _bag, start, slug = job
        ev = fetch_event(http, slug)
        if not ev or not ev.get("condition"):
            return name, ev, []
        t = fetch_trades(http, ev["condition"], start, start + 5 * 60 + 30, args.cache)
        return name, ev, t

    p("fetching", len(jobs), "5m Gamma events + last-trade prints (public, no keys)")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(load_one, job) for job in jobs]
        done = 0
        for fut in as_completed(futs):
            name, ev, t = fut.result()
            events[name] = ev
            trades[name] = t
            done += 1
            if done % 25 == 0:
                print(f"  fetched {done}/{len(jobs)}", flush=True)

    scored: list[dict] = []
    for name, bag, start, slug in jobs:
        buys = sorted(bag["buys"], key=lambda r: r["ts"])
        first = buys[0]
        held = first["leg"].lower()
        if held not in ("up", "down"):
            continue
        ev = events.get(name) or {}
        winner = ev.get("winner")
        if winner not in ("up", "down"):
            if bag["redeems"]:
                winner = bag["redeems"][0]["leg"].lower()
            elif not ev.get("closed"):
                winner = None
            else:
                winner = "down" if held == "up" else "up"
        if winner is None:
            continue
        path = prints_for(trades.get(name) or [], held, first["ts"])
        first_n = first["usdc"]
        first_sh = first["tok"]
        all_n = sum(b["usdc"] for b in buys)
        all_sh = sum(b["tok"] for b in buys)
        won = held == winner
        min_px = min((px for _, px in path), default=None)
        same_leg = all(b["leg"].lower() == held for b in buys)
        kind = "one" if len(buys) == 1 else ("two_same" if same_leg else "other_leg")
        row = {
            "name": name,
            "slug": slug,
            "held": held,
            "winner": winner,
            "won": won,
            "kind": kind,
            "n_buys": len(buys),
            "first_px": first["px"],
            "first_ts": first["ts"],
            "second_ts": buys[1]["ts"] if len(buys) > 1 else None,
            "second_px": buys[1]["px"] if len(buys) > 1 else None,
            "first_n": first_n,
            "first_sh": first_sh,
            "all_n": all_n,
            "all_sh": all_sh,
            "prints": len(path),
            "min_px": min_px,
            "trade_n": len(trades.get(name) or []),
        }
        for thr in THRESHOLDS:
            hit = first_touch(path, thr)
            row[f"t{int(thr * 100)}"] = hit
            for persist in PERSIST_S:
                row[f"t{int(thr * 100)}_p{int(persist)}"] = persist_touch(path, thr, persist)
        for drop in DROPS:
            row[f"d{int(drop * 100)}"] = fade_touch(path, float(first["px"] or 0), drop)
        if first["px"] and first["px"] >= 0.90 - 1e-12:
            fade = None
            for ts, px in path:
                if 0.75 - 1e-12 <= px <= 0.90 + 1e-12:
                    fade = (ts, px)
                    break
            row["fade_7590"] = fade
        else:
            row["fade_7590"] = None
        scored.append(row)

    decided = scored
    winners = [r for r in decided if r["won"]]
    losers = [r for r in decided if not r["won"]]
    two = [r for r in decided if r["kind"] == "two_same"]
    p()
    p("=== SAMPLE ===")
    p(
        f"5m decided markets {len(decided)}  winners {len(winners)}  losers {len(losers)}  "
        f"two-same-leg {len(two)}  other-leg {sum(1 for r in decided if r['kind']=='other_leg')}"
    )
    p(
        f"prints after first buy: median "
        f"{sorted(r['prints'] for r in decided)[len(decided)//2] if decided else 0}  "
        f"markets with 0 prints {sum(1 for r in decided if r['prints']==0)}"
    )
    p(
        "Last-trade caveats: a 70¢ print is not a 70¢ bid; a gap-down first "
        "print of 40¢ means 70 never traded. Empty FAK not modeled."
    )
    p()

    p("=== DID THE HELD LEG EVER PRINT ≤ T AFTER OUR FIRST BUY? ===")
    p("T    winners_hit  losers_hit  winner_FP  loser_cover  gap_thru(<T-10¢)  two_hit_before_2nd")
    for thr in THRESHOLDS:
        key = f"t{int(thr * 100)}"
        w_hit = sum(1 for r in winners if r[key])
        l_hit = sum(1 for r in losers if r[key])
        gap = sum(
            1
            for r in decided
            if r[key] and r[key][1] < thr - 0.10
        )
        before2 = 0
        for r in two:
            hit = r[key]
            if hit and r["second_ts"] and hit[0] < r["second_ts"]:
                before2 += 1
        p(
            f"{thr:.2f}  {w_hit:3d}/{len(winners):<3d}     {l_hit:3d}/{len(losers):<3d}     "
            f"{wr(w_hit, len(winners)):6s}     {wr(l_hit, len(losers)):6s}      "
            f"{gap:3d}               {before2}/{len(two)}"
        )
    p(
        "winner_FP = fraction of *winning* bags that would have been sold. "
        "loser_cover = fraction of losing bags the stop would have seen."
    )
    p()

    p("=== FIRST PRINT AT/BELOW T (may gap through) ===")
    p("T    n  median_px  p10  p90  mean_px")
    for thr in THRESHOLDS:
        key = f"t{int(thr * 100)}"
        pxs = [r[key][1] for r in decided if r[key]]
        if not pxs:
            p(f"{thr:.2f}  0")
            continue
        pxs.sort()
        p(
            f"{thr:.2f}  {len(pxs):3d}  {pxs[len(pxs)//2]:.3f}      "
            f"{pxs[max(0,len(pxs)//10)]:.3f}  {pxs[min(len(pxs)-1, 9*len(pxs)//10)]:.3f}  "
            f"{sum(pxs)/len(pxs):.3f}"
        )
    p()

    p("=== PAPER P&L — FIRST SLICE ONLY (ignore the add) ===")
    p("rule                         n   dumps  winner_dumps  pnl")

    def table_row(label: str, pnls: list[float], dumps: int, wdumps: int) -> None:
        p(
            f"{label:<28s} {len(pnls):3d}  {dumps:5d}  {wdumps:12d}  {sum(pnls):+8.2f}"
        )

    ride = [paper_exit(r["first_n"], r["first_sh"], None, r["won"]) for r in decided]
    table_row("ride_to_1_or_0", ride, 0, 0)
    for thr in THRESHOLDS:
        key = f"t{int(thr * 100)}"
        pnls = []
        dumps = wdumps = 0
        for r in decided:
            hit = r[key]
            if hit:
                pnls.append(paper_exit(r["first_n"], r["first_sh"], hit[1], r["won"]))
                dumps += 1
                if r["won"]:
                    wdumps += 1
            else:
                pnls.append(paper_exit(r["first_n"], r["first_sh"], None, r["won"]))
        table_row(f"sell_first_print<={thr:.2f}", pnls, dumps, wdumps)
    for persist in PERSIST_S:
        for thr in (0.70, 0.65, 0.60):
            key = f"t{int(thr * 100)}_p{int(persist)}"
            pnls = []
            dumps = wdumps = 0
            for r in decided:
                hit = r[key]
                if hit:
                    pnls.append(paper_exit(r["first_n"], r["first_sh"], hit[1], r["won"]))
                    dumps += 1
                    if r["won"]:
                        wdumps += 1
                else:
                    pnls.append(paper_exit(r["first_n"], r["first_sh"], None, r["won"]))
            table_row(f"persist{int(persist)}s<={thr:.2f}", pnls, dumps, wdumps)
    for drop in DROPS:
        key = f"d{int(drop * 100)}"
        pnls = []
        dumps = wdumps = 0
        for r in decided:
            hit = r[key] or r["t53"]
            if r[key] or r["t53"]:
                px = hit[1]
                pnls.append(paper_exit(r["first_n"], r["first_sh"], px, r["won"]))
                dumps += 1
                if r["won"]:
                    wdumps += 1
            else:
                pnls.append(paper_exit(r["first_n"], r["first_sh"], None, r["won"]))
        table_row(f"fade{int(drop*100)}c_or_53", pnls, dumps, wdumps)
    p()

    p("=== PAPER P&L — FULL BAG (adds stay if we do not sell first) ===")
    p("rule                         n   dumps  winner_dumps  pnl")
    ride_all = [paper_exit(r["all_n"], r["all_sh"], None, r["won"]) for r in decided]
    table_row("ride_full_bag", ride_all, 0, 0)
    for thr in (0.70, 0.65, 0.60, 0.53):
        key = f"t{int(thr * 100)}"
        pnls = []
        dumps = wdumps = 0
        for r in decided:
            hit = r[key]
            if hit and (r["second_ts"] is None or hit[0] <= r["second_ts"]):
                pnls.append(paper_exit(r["first_n"], r["first_sh"], hit[1], r["won"]))
                dumps += 1
                if r["won"]:
                    wdumps += 1
            elif hit:
                pnls.append(paper_exit(r["all_n"], r["all_sh"], hit[1], r["won"]))
                dumps += 1
                if r["won"]:
                    wdumps += 1
            else:
                pnls.append(paper_exit(r["all_n"], r["all_sh"], None, r["won"]))
        table_row(f"sell<={thr:.2f}_before_add", pnls, dumps, wdumps)
    p(
        "sell<=T_before_add: if the stop prints before the second fill, the add never happens."
    )
    p()

    p("=== TWO-SAME-LEG FADES (the 64% book) ===")
    p(f"n={len(two)}  wins={sum(1 for r in two if r['won'])}  "
      f"ride_full {sum(paper_exit(r['all_n'], r['all_sh'], None, r['won']) for r in two):+.2f}  "
      f"first_only {sum(paper_exit(r['first_n'], r['first_sh'], None, r['won']) for r in two):+.2f}")
    if two:
        p("T    hit  before_2nd  winner_hit  loser_hit  first_print_med")
        for thr in THRESHOLDS:
            key = f"t{int(thr * 100)}"
            hits = [r for r in two if r[key]]
            before = [
                r for r in hits if r["second_ts"] and r[key][0] < r["second_ts"]
            ]
            pxs = sorted(r[key][1] for r in hits)
            p(
                f"{thr:.2f}  {len(hits):3d}  {len(before):3d}         "
                f"{sum(1 for r in hits if r['won']):3d}         "
                f"{sum(1 for r in hits if not r['won']):3d}       "
                f"{(pxs[len(pxs)//2] if pxs else 0):.3f}"
            )
        fade_n = sum(1 for r in two if r.get("fade_7590"))
        p(f"held last-print entered 75–90 after a ≥90 first fill: {fade_n}/{len(two)}")
    p()

    p("=== LOSER PATHS (when did they first print 70 / 65 / 53?) ===")
    for r in sorted(losers, key=lambda x: x["all_n"], reverse=True)[:18]:
        t70 = r["t70"]
        t65 = r["t65"]
        t53 = r["t53"]
        p(
            f"{r['name'][-28:]:<28s} {r['kind']:<9s} fill {r['first_px'] or 0:.2f}  "
            f"min {r['min_px'] if r['min_px'] is not None else float('nan'):.2f}  "
            f"70={t70[1] if t70 else '-':>5}  "
            f"65={t65[1] if t65 else '-':>5}  "
            f"53={t53[1] if t53 else '-':>5}  "
            f"lead70 {((t53[0]-t70[0]) if t70 and t53 else 0):4.0f}s"
        )
    p()
    p(
        "If loser_cover at 70 ≈ loser_cover at 53 and winner_FP at 70 is high, "
        "raising the stop sells winners without catching more losers. "
        "If losers print 70 long before 53 and winners rarely do, 70 is the better trigger."
    )

    text = "\n".join(lines) + "\n"
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
