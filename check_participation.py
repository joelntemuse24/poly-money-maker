#!/usr/bin/env python3
"""Post-facto participation autopsy: bought vs missed BTC Up/Down markets.

Read-only. Does not change bot logic or place orders.

Joins:
  - theoretical market calendar (Gamma series events)
  - buys from Polymarket history CSV and/or bot logs / research / pnl
  - CLOB prices-history in each bot's buy window (band exposure on misses)
  - named buy_skip_* events from logs when present

Usage (on the VM, from the repo root):
  python check_participation.py --hours 3
  python check_participation.py --bot 5m --hours 6 --csv ~/Downloads/history.csv
  python check_participation.py --start-ts 1786508400 --end-ts 1786512000
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

BOTS = {
    "5m": {
        "series_slug": "btc-up-or-down-5m",
        "log": "buybot5m.log",
        "research": "underlying_research_buy5m.jsonl",
        "pnl": "pnl_buy5m.json",
        "window_s": 120.0,
        "duration_s": 5 * 60,
    },
    "15m": {
        "series_slug": "btc-up-or-down-15m",
        "log": "buybot.log",
        "research": "underlying_research_buy.jsonl",
        "pnl": "pnl_buy.json",
        "window_s": 4.0 * 60,
        "duration_s": 15 * 60,
    },
    "hr": {
        "series_slug": "btc-up-or-down-hourly",
        "log": "buybothourly.log",
        "research": "underlying_research_buyhourly.jsonl",
        "pnl": "pnl_buyhourly.json",
        "window_s": 13.0 * 60,
        "duration_s": 60 * 60,
    },
}

BUY_EVENTS = frozenset({"buy_fill", "buy_attempt", "buy_ghost_fill"})
SKIP_PREFIX = "buy_skip_"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "poly-money-maker-participation-autopsy"
    return s


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


def _norm_q(text: str) -> str:
    s = (text or "").lower()
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9 :.\-apm]", "", s)
    return s.strip()


@dataclass
class Market:
    condition_id: str
    slug: str
    question: str
    start_ts: float
    end_ts: float
    up_token: str
    dn_token: str
    bot: str


@dataclass
class BuyHit:
    sources: Set[str] = field(default_factory=set)
    avg_price: Optional[float] = None
    usdc: Optional[float] = None
    leg: Optional[str] = None
    ts: Optional[float] = None
    detail: str = ""


def enumerate_markets(
    session: requests.Session,
    bot: str,
    cfg: dict,
    start_ts: float,
    end_ts: float,
    sleep_s: float,
) -> List[Market]:
    """Page Gamma events for the series; keep markets whose end falls in range."""
    out: Dict[str, Market] = {}
    series = cfg["series_slug"]
    duration = float(cfg["duration_s"])
    for closed in ("true", "false"):
        offset = 0
        for _ in range(40):  # hard cap pages
            r = session.get(
                f"{GAMMA}/events",
                params={
                    "series_slug": series,
                    "closed": closed,
                    "limit": 50,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "false",
                },
                timeout=20,
            )
            if sleep_s:
                time.sleep(sleep_s)
            if r.status_code != 200:
                break
            events = r.json() or []
            if not events:
                break
            oldest_end = None
            for event in events:
                for raw in event.get("markets") or []:
                    m = _market_from_gamma(raw, event, bot, duration)
                    if m is None:
                        continue
                    oldest_end = m.end_ts if oldest_end is None else min(oldest_end, m.end_ts)
                    if m.end_ts < start_ts or m.end_ts > end_ts:
                        continue
                    out[m.condition_id] = m
            offset += len(events)
            # Pages are newest-first; stop once we've scrolled past the window.
            if oldest_end is not None and oldest_end < start_ts:
                break
            if len(events) < 50:
                break
    return sorted(out.values(), key=lambda m: m.end_ts)


def _market_from_gamma(raw: dict, event: dict, bot: str, duration: float) -> Optional[Market]:
    cid = str(raw.get("conditionId") or raw.get("condition_id") or "")
    tokens = _parse_json_field(raw.get("clobTokenIds")) or []
    outcomes = [str(o).lower() for o in (_parse_json_field(raw.get("outcomes")) or [])]
    if not cid or len(tokens) != 2 or len(outcomes) != 2:
        return None
    mapping = dict(zip(outcomes, (str(t) for t in tokens)))
    if "up" not in mapping or "down" not in mapping:
        return None
    end = _end_ts(raw.get("endDate") or event.get("endDate"))
    if end is None:
        return None
    slug = str(raw.get("slug") or event.get("slug") or cid)
    m_ts = re.search(r"-(\d{10,})$", slug)
    if m_ts and int(m_ts.group(1)) > 1_700_000_000:
        start = float(int(m_ts.group(1)))
    else:
        start = end - duration
    if abs((end - start) - duration) > max(5.0, duration * 0.05):
        start = end - duration
    return Market(
        condition_id=cid,
        slug=slug,
        question=str(raw.get("question") or event.get("title") or ""),
        start_ts=start,
        end_ts=end,
        up_token=mapping["up"],
        dn_token=mapping["down"],
        bot=bot,
    )


def load_jsonl_events(paths: Sequence[str]) -> List[dict]:
    rows: List[dict] = []
    for path in paths:
        try:
            handle = open(path, encoding="utf-8")
        except FileNotFoundError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rows.append(json.loads(line))
                except (TypeError, ValueError):
                    continue
    return rows


def load_bot_buys(
    cfg: dict,
    start_ts: float,
    end_ts: float,
) -> Tuple[Dict[str, BuyHit], Dict[str, List[str]]]:
    """condition_id -> BuyHit; condition_id -> skip event names."""
    buys: Dict[str, BuyHit] = {}
    skips: Dict[str, List[str]] = defaultdict(list)
    events = load_jsonl_events([cfg["log"], cfg["research"]])

    for ev in events:
        name = str(ev.get("event") or "")
        cid = str(ev.get("condition_id") or "")
        ts = ev.get("ts") or ev.get("timestamp") or ev.get("t")
        try:
            ts_f = float(ts) if ts is not None else None
        except (TypeError, ValueError):
            ts_f = None
        # log timestamps are often ms
        if ts_f is not None and ts_f > 1e12:
            ts_f /= 1000.0
        if ts_f is not None and (ts_f < start_ts - 3600 or ts_f > end_ts + 3600):
            # keep loose; market end filter applied later via cid join
            pass

        if name in BUY_EVENTS and cid:
            hit = buys.setdefault(cid, BuyHit())
            hit.sources.add("log" if "log" in str(cfg["log"]) else "bot")
            hit.sources.add("bot")
            if ev.get("avg_price") is not None:
                try:
                    hit.avg_price = float(ev["avg_price"])
                except (TypeError, ValueError):
                    pass
            elif ev.get("ask") is not None and hit.avg_price is None:
                try:
                    hit.avg_price = float(ev["ask"])
                except (TypeError, ValueError):
                    pass
            hit.leg = ev.get("leg") or hit.leg
            hit.ts = ts_f or hit.ts
            hit.detail = name

        if name.startswith(SKIP_PREFIX) and cid:
            if name not in skips[cid]:
                skips[cid].append(name)

    # pnl file: best-effort — structure varies; look for fills / entries
    pnl_path = Path(cfg["pnl"])
    if pnl_path.is_file():
        try:
            pnl = json.loads(pnl_path.read_text(encoding="utf-8"))
        except (TypeError, ValueError, OSError):
            pnl = None
        for cid, hit in _buys_from_pnl(pnl).items():
            dest = buys.setdefault(cid, BuyHit())
            dest.sources.add("pnl")
            dest.sources.update(hit.sources)
            if hit.avg_price is not None:
                dest.avg_price = hit.avg_price
            if hit.leg:
                dest.leg = hit.leg
            if hit.usdc is not None:
                dest.usdc = hit.usdc
            if hit.ts is not None:
                dest.ts = hit.ts

    return buys, skips


def _buys_from_pnl(pnl: Any) -> Dict[str, BuyHit]:
    out: Dict[str, BuyHit] = {}
    if not isinstance(pnl, dict):
        return out

    def consider(cid: str, row: dict) -> None:
        if not cid:
            return
        hit = out.setdefault(cid, BuyHit(sources={"pnl"}))
        for key in ("avg_price", "entry_price", "price", "buy_avg"):
            if row.get(key) is not None:
                try:
                    hit.avg_price = float(row[key])
                    break
                except (TypeError, ValueError):
                    pass
        for key in ("spent", "usdc", "cost", "notional"):
            if row.get(key) is not None:
                try:
                    hit.usdc = float(row[key])
                    break
                except (TypeError, ValueError):
                    pass
        hit.leg = row.get("leg") or row.get("side") or hit.leg

    # common shapes: {"fills":[{condition_id:...}]}, {"entries":{cid:{...}}}, flat cid keys
    for key in ("fills", "entries", "buys", "trades"):
        block = pnl.get(key)
        if isinstance(block, list):
            for row in block:
                if isinstance(row, dict):
                    consider(str(row.get("condition_id") or row.get("cid") or ""), row)
        elif isinstance(block, dict):
            for cid, row in block.items():
                if isinstance(row, dict):
                    consider(str(cid), row)
    for cid, row in pnl.items():
        if isinstance(row, dict) and (
            "avg_price" in row or "entry_price" in row or "bought" in row or "shares" in row
        ):
            consider(str(cid), row)
    return out


def load_csv_buys(path: Optional[str]) -> List[dict]:
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        print(f"  WARNING: csv not found: {path}")
        return []
    text = p.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(text.splitlines()))
    # normalize header
    for r in rows:
        for k in list(r):
            if "marketName" in k and k != "marketName":
                r["marketName"] = r.pop(k)
    out = []
    for r in rows:
        if str(r.get("action") or "").strip().lower() != "buy":
            continue
        try:
            ts = float(r["timestamp"])
            usdc = float(r["usdcAmount"])
            tokens = float(r["tokenAmount"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            {
                "ts": ts,
                "market": r.get("marketName") or "",
                "leg": (r.get("tokenName") or "").strip().lower() or None,
                "usdc": usdc,
                "tokens": tokens,
                "avg": (usdc / tokens) if tokens else None,
                "norm": _norm_q(r.get("marketName") or ""),
            }
        )
    return out


def match_csv_to_markets(
    csv_buys: List[dict],
    markets: List[Market],
) -> Dict[str, BuyHit]:
    by_norm: Dict[str, List[Market]] = defaultdict(list)
    for m in markets:
        by_norm[_norm_q(m.question)].append(m)

    hits: Dict[str, BuyHit] = {}
    for b in csv_buys:
        cands = by_norm.get(b["norm"]) or []
        if not cands:
            # fuzzy: strip "bitcoin up or down -"
            alt = re.sub(r"^bitcoin up or down\s*-?\s*", "", b["norm"]).strip()
            for q, ms in by_norm.items():
                if alt and alt in q:
                    cands = ms
                    break
        if not cands:
            continue
        # pick market whose window contains buy ts, else nearest end
        ts = b["ts"]
        best = min(cands, key=lambda m: abs(m.end_ts - ts))
        if ts < best.start_ts - 120 or ts > best.end_ts + 600:
            # still accept nearest if within an hour of end (redeem lag not relevant for buys)
            if abs(best.end_ts - ts) > 3600:
                continue
        hit = hits.setdefault(best.condition_id, BuyHit())
        hit.sources.add("csv")
        hit.avg_price = b.get("avg")
        hit.usdc = b.get("usdc")
        hit.leg = b.get("leg")
        hit.ts = ts
        hit.detail = "csv_buy"
    return hits


def prices_history(
    session: requests.Session,
    token_id: str,
    start_ts: float,
    end_ts: float,
    fidelity: int,
    sleep_s: float,
) -> List[Tuple[float, float]]:
    r = session.get(
        f"{CLOB}/prices-history",
        params={
            "market": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity,
        },
        timeout=20,
    )
    if sleep_s:
        time.sleep(sleep_s)
    if r.status_code != 200:
        return []
    data = r.json()
    hist = data.get("history") if isinstance(data, dict) else data
    out: List[Tuple[float, float]] = []
    for pt in hist or []:
        try:
            out.append((float(pt["t"]), float(pt["p"])))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def classify_band(
    points: Sequence[float],
    lo: float,
    hi: float,
) -> str:
    if not points:
        return "no_history"
    mn, mx = min(points), max(points)
    in_band = any(lo - 1e-12 <= p <= hi + 1e-12 for p in points)
    if in_band:
        return "saw_in_band"
    if mx < lo:
        return "never_reached_trigger"
    if mn > hi:
        return "above_ceiling_only"
    # max >= lo and min <= hi but no point inside — treat as gap / sparse
    if mx > hi and mn < lo:
        return "gapped_through_band"
    return "unclear"


def analyze_miss(
    session: requests.Session,
    market: Market,
    window_s: float,
    lo: float,
    hi: float,
    fidelity: int,
    sleep_s: float,
) -> dict:
    w0 = max(market.start_ts, market.end_ts - window_s)
    w1 = market.end_ts
    up = prices_history(session, market.up_token, w0, w1, fidelity, sleep_s)
    dn = prices_history(session, market.dn_token, w0, w1, fidelity, sleep_s)
    up_ps = [p for _, p in up]
    dn_ps = [p for _, p in dn]
    # Opportunity on either leg (winner usually the high one).
    labels = {
        "up": classify_band(up_ps, lo, hi),
        "down": classify_band(dn_ps, lo, hi),
    }
    # Primary: prefer saw_in_band on either; else best-effort from the higher peak leg.
    if "saw_in_band" in labels.values():
        primary = "saw_in_band"
    elif labels["up"] == "no_history" and labels["down"] == "no_history":
        primary = "no_history"
    else:
        peak_up = max(up_ps) if up_ps else -1.0
        peak_dn = max(dn_ps) if dn_ps else -1.0
        primary = labels["up"] if peak_up >= peak_dn else labels["down"]
    return {
        "primary": primary,
        "up_label": labels["up"],
        "dn_label": labels["down"],
        "up_min": min(up_ps) if up_ps else None,
        "up_max": max(up_ps) if up_ps else None,
        "dn_min": min(dn_ps) if dn_ps else None,
        "dn_max": max(dn_ps) if dn_ps else None,
        "window_start": w0,
        "n_up": len(up_ps),
        "n_dn": len(dn_ps),
    }


def merge_buys(*maps: Dict[str, BuyHit]) -> Dict[str, BuyHit]:
    out: Dict[str, BuyHit] = {}
    for m in maps:
        for cid, hit in m.items():
            dest = out.setdefault(cid, BuyHit())
            dest.sources.update(hit.sources)
            if hit.avg_price is not None:
                dest.avg_price = hit.avg_price
            if hit.usdc is not None:
                dest.usdc = hit.usdc
            if hit.leg:
                dest.leg = hit.leg
            if hit.ts is not None:
                dest.ts = hit.ts
            if hit.detail:
                dest.detail = hit.detail
    return out


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def run_bot(
    session: requests.Session,
    bot: str,
    cfg: dict,
    start_ts: float,
    end_ts: float,
    csv_buys: List[dict],
    lo: float,
    hi: float,
    fidelity: int,
    sleep_s: float,
    show_rows: int,
) -> None:
    print(f"\n===== {bot}  window={cfg['window_s']:.0f}s  band={lo:.2f}-{hi:.2f} =====")
    markets = enumerate_markets(session, bot, cfg, start_ts, end_ts, sleep_s)
    print(f"markets ending in range: {len(markets)}")
    if not markets:
        return

    bot_buys, skips = load_bot_buys(cfg, start_ts, end_ts)
    # tag bot source cleanly
    for hit in bot_buys.values():
        hit.sources.discard("log")
        hit.sources.add("bot")
    csv_hits = match_csv_to_markets(csv_buys, markets)
    buys = merge_buys(bot_buys, csv_hits)

    bought_rows = []
    miss_rows = []
    labels = Counter()
    source_counts = Counter()

    for m in markets:
        hit = buys.get(m.condition_id)
        # also match bot buys that only have cid from overlapping research
        if hit and hit.sources:
            src = "+".join(sorted(hit.sources))
            source_counts[src] += 1
            bought_rows.append((m, hit, src))
            labels["bought"] += 1
            continue

        info = analyze_miss(session, m, float(cfg["window_s"]), lo, hi, fidelity, sleep_s)
        skip_names = skips.get(m.condition_id) or []
        primary = info["primary"]
        if skip_names and primary in ("saw_in_band", "unclear", "gapped_through_band"):
            # annotate but keep band label as primary for velocity thesis
            pass
        labels[primary] += 1
        miss_rows.append((m, info, skip_names))

    n = len(markets)
    n_bought = labels["bought"]
    print(f"bought: {n_bought}/{n}  ({(100.0 * n_bought / n) if n else 0:.1f}%)")
    if source_counts:
        print("  buy sources:", dict(source_counts))
    print("miss / other labels:")
    for k, v in labels.most_common():
        if k == "bought":
            continue
        print(f"  {k:24} {v}")

    # Known skips on bought+missed
    skip_on_miss = Counter()
    for _, _, sk in miss_rows:
        for s in sk:
            skip_on_miss[s] += 1
    if skip_on_miss:
        print("named log skips on missed markets:")
        for k, v in skip_on_miss.most_common(12):
            print(f"  {k:32} {v}")

    if show_rows > 0:
        print(f"\n--- sample bought (up to {show_rows}) ---")
        for m, hit, src in bought_rows[:show_rows]:
            avg = f"{hit.avg_price:.3f}" if hit.avg_price is not None else "?"
            print(
                f"  BUY  {fmt_ts(m.end_ts)}  avg={avg:>5}  src={src:9}  {m.question[:64]}"
            )
        print(f"\n--- sample misses (up to {show_rows}) ---")
        # prioritize saw_in_band then above_ceiling
        order = {
            "saw_in_band": 0,
            "gapped_through_band": 1,
            "above_ceiling_only": 2,
            "never_reached_trigger": 3,
            "unclear": 4,
            "no_history": 5,
        }
        miss_sorted = sorted(
            miss_rows, key=lambda row: (order.get(row[1]["primary"], 9), row[0].end_ts)
        )
        for m, info, sk in miss_sorted[:show_rows]:
            up = info["up_max"]
            dn = info["dn_max"]
            up_s = f"{up:.3f}" if up is not None else "na"
            dn_s = f"{dn:.3f}" if dn is not None else "na"
            sk_s = ",".join(sk[:2]) if sk else "-"
            print(
                f"  MISS {fmt_ts(m.end_ts)}  {info['primary']:22}  "
                f"up_max={up_s} dn_max={dn_s}  skips={sk_s}  {m.question[:52]}"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bot", choices=["5m", "15m", "hr", "all"], default="all")
    ap.add_argument("--hours", type=float, default=3.0, help="lookback hours ending now")
    ap.add_argument("--start-ts", type=float, default=None)
    ap.add_argument("--end-ts", type=float, default=None)
    ap.add_argument("--csv", type=str, default=None, help="Polymarket history CSV path")
    ap.add_argument("--threshold", type=float, default=0.75)
    ap.add_argument("--max-price", type=float, default=0.90)
    ap.add_argument("--fidelity", type=int, default=1, help="prices-history fidelity minutes")
    ap.add_argument("--sleep", type=float, default=0.05, help="pause between HTTP calls")
    ap.add_argument("--show", type=int, default=15, help="sample rows to print")
    ap.add_argument("--window-5m", type=float, default=None)
    ap.add_argument("--window-15m", type=float, default=None)
    ap.add_argument("--window-hr", type=float, default=None)
    args = ap.parse_args()

    end_ts = args.end_ts if args.end_ts is not None else time.time()
    start_ts = args.start_ts if args.start_ts is not None else end_ts - args.hours * 3600
    if start_ts >= end_ts:
        raise SystemExit("start-ts must be < end-ts")

    bots = list(BOTS) if args.bot == "all" else [args.bot]
    if args.window_5m is not None:
        BOTS["5m"]["window_s"] = float(args.window_5m)
    if args.window_15m is not None:
        BOTS["15m"]["window_s"] = float(args.window_15m)
    if args.window_hr is not None:
        BOTS["hr"]["window_s"] = float(args.window_hr)

    print(
        f"participation autopsy  {fmt_ts(start_ts)} → {fmt_ts(end_ts)}  "
        f"band=[{args.threshold}, {args.max_price}]"
    )
    csv_buys = load_csv_buys(args.csv)
    if args.csv:
        print(f"csv buys loaded: {len(csv_buys)} from {args.csv}")

    session = _session()
    for bot in bots:
        run_bot(
            session,
            bot,
            BOTS[bot],
            start_ts,
            end_ts,
            csv_buys,
            args.threshold,
            args.max_price,
            args.fidelity,
            args.sleep,
            args.show,
        )


if __name__ == "__main__":
    main()
