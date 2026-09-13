#!/usr/bin/env python3
"""Stratified multi-regime win-rate panel for BTC 15m Up/Down ≥95¢ last-WINDOW.

Live rule studied (research only — no orders, no strategy knob changes):
  In the last WINDOW seconds of each 15m BTC Up/Down market, take the FIRST
  opportunity where either Up or Down last-print (default: denser Data API
  /trades; optional: CLOB prices-history mid) ≥ 0.95. Hold to resolve. Also
  report a side panel for first touch in [0.90, 0.949].

Window sweep: WINDOW_S ∈ {180, 300, 480} (3m / 5m / 8m).

Reuses Gamma series paging from check_92c_week_backtest.enumerate_gamma /
SERIES["15m"] (series_slug=btc-up-or-down-15m). Does NOT use hourly ET slug
candidate lists — sparse windows are filled via unix slug prefixes
btc-updown-15m-{unix} and btc-up-or-down-15m-{unix}.

Usage (on poly-vm, from repo root):
  .venv/bin/python check_15m_regime_panel.py
  .venv/bin/python check_15m_regime_panel.py --windows W11,W12
  .venv/bin/python check_15m_regime_panel.py --buy-windows 180,300
  .venv/bin/python check_15m_regime_panel.py --rebuild-index
  .venv/bin/python check_15m_regime_panel.py --source clob --export-tag clob \
      --windows W06,W07,W09,W10,W11,W12

Price sources (default = trades / denser Data API tape — CANONICAL):
  trades — Data API /trades last-print paths (~1–3s median unique-second gaps)
  clob   — CLOB /prices-history fidelity minutes (floor ≈1m; cannot do 15s)

Writes (default source=trades, default tag empty = main/canonical panel):
  exports/15m_regime_panel[_TAG]_summary.csv
  exports/15m_regime_panel[_TAG]_entries.csv
  exports/15m_regime_panel[_TAG]_report.json
  buy_data_15m/regime_panel_market_index.json
  Canonical denser summary: exports/15m_regime_panel_CANONICAL.md
  Legacy CLOB coarseness: pass --source clob (auto-tag clob if tag empty).
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REPO = Path(__file__).resolve().parent
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://api.binance.com/api/v3/klines"
SERIES_SLUG = "btc-up-or-down-15m"
SLUG_PREFIXES = ("btc-updown-15m-", "btc-up-or-down-15m-")
USER_AGENT = "poly-money-maker-15m-regime-panel/1.0"
ET = ZoneInfo("America/New_York")

DURATION_S = 900
BUY_WINDOWS = (180, 300, 480)  # 3m / 5m / 8m
HI_95 = 0.95
LO_90 = 0.90
HI_90 = 0.949999
HEDGE_THRESHOLDS = (0.35, 0.40, 0.60)

INDEX_PATH = REPO / "buy_data_15m" / "regime_panel_market_index.json"
EXPORT_DIR = REPO / "exports"
CACHE_DIR = Path("/tmp/poly_15m_regime_panel_ph")
TRADES_URL = "https://data-api.polymarket.com/trades"
TRADES_CACHE_DIR = Path("/tmp/poly_15m_regime_panel_trades")

# Same calendar/regime windows as hourly panel (ET inclusive).
WINDOWS: List[Dict[str, Any]] = [
    {
        "id": "W01",
        "start": "2026-02-23",
        "end": "2026-02-25",
        "regime": "down_then_up_highvol",
        "dir": "mixed",
        "vol": "high",
        "notes": "Feb23 dump then Feb25 rip; high range",
    },
    {
        "id": "W02",
        "start": "2026-03-02",
        "end": "2026-03-06",
        "regime": "up_then_down_highvol",
        "dir": "mixed",
        "vol": "high",
        "notes": "Mar2-4 strong up then Mar6 fade",
    },
    {
        "id": "W03",
        "start": "2026-03-26",
        "end": "2026-03-28",
        "regime": "down_medvol",
        "dir": "down",
        "vol": "med",
        "notes": "Mar26-27 consecutive down days",
    },
    {
        "id": "W04",
        "start": "2026-04-12",
        "end": "2026-04-14",
        "regime": "up_medvol",
        "dir": "up",
        "vol": "med",
        "notes": "Apr13 +5.2% up day",
    },
    {
        "id": "W05",
        "start": "2026-04-24",
        "end": "2026-04-26",
        "regime": "chop_lowvol",
        "dir": "chop",
        "vol": "low",
        "notes": "Apr25 near-flat low range",
    },
    {
        "id": "W06",
        "start": "2026-06-02",
        "end": "2026-06-05",
        "regime": "down_highvol",
        "dir": "down",
        "vol": "high",
        "notes": "Jun2 -6.5% then Jun5 -4.4%",
    },
    {
        "id": "W07",
        "start": "2026-06-23",
        "end": "2026-06-25",
        "regime": "down_highvol",
        "dir": "down",
        "vol": "high",
        "notes": "Jun23-25 multi-day selloff, high range",
    },
    {
        "id": "W08",
        "start": "2026-07-11",
        "end": "2026-07-13",
        "regime": "chop_lowvol",
        "dir": "chop",
        "vol": "low",
        "notes": "Jul12 flat low range",
    },
    {
        "id": "W09",
        "start": "2026-07-27",
        "end": "2026-07-31",
        "regime": "down_medvol",
        "dir": "down",
        "vol": "med",
        "notes": "Jul27/31 down days",
    },
    {
        "id": "W10",
        "start": "2026-08-08",
        "end": "2026-08-10",
        "regime": "chop_lowvol",
        "dir": "chop",
        "vol": "low",
        "notes": "Aug8-9 very quiet",
    },
    {
        "id": "W11",
        "start": "2026-08-19",
        "end": "2026-08-22",
        "regime": "up_highvol",
        "dir": "up",
        "vol": "high",
        "notes": "Aug19-21 strong multi-day rip",
    },
    {
        "id": "W12",
        "start": "2026-08-29",
        "end": "2026-09-05",
        "regime": "recent_baseline",
        "dir": "upish",
        "vol": "med",
        "notes": "Recent baseline (Aug29-Sep5)",
    },
]


def session() -> requests.Session:
    out = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.5,
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


@dataclass
class MarketMeta:
    condition_id: str
    slug: str
    start_ts: float
    end_ts: float
    up_token: str
    dn_token: str
    winner: Optional[str]
    closed: bool


def market_from_gamma(raw: dict, event: dict) -> Optional[MarketMeta]:
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
    start = end - DURATION_S
    if "-" in slug:
        tail = slug.rsplit("-", 1)[-1]
        if tail.isdigit() and int(tail) > 1_700_000_000:
            start = float(int(tail))
            # Prefer duration from start if end looks off
            if abs((start + DURATION_S) - end) > 120:
                end = start + DURATION_S
    return MarketMeta(
        condition_id=cid,
        slug=slug,
        start_ts=start,
        end_ts=end,
        up_token=mapping["up"],
        dn_token=mapping["down"],
        winner=winner_from_market(raw),
        closed=bool(raw.get("closed")),
    )


def et_day_bounds(day: str) -> Tuple[float, float]:
    y, m, d = map(int, day.split("-"))
    start = datetime(y, m, d, 0, 0, tzinfo=ET)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def window_ts_range(w: dict) -> Tuple[float, float]:
    s0, _ = et_day_bounds(w["start"])
    _, e1 = et_day_bounds(w["end"])
    return s0, e1


def build_market_index(
    http: requests.Session,
    start_ts: float,
    end_ts: float,
    sleep_s: float,
) -> List[MarketMeta]:
    """Gamma events?series_slug=btc-up-or-down-15m (same pattern as enumerate_gamma)."""
    out: Dict[str, MarketMeta] = {}
    for closed in ("true", "false"):
        offset = 0
        for _ in range(400):
            resp = http.get(
                f"{GAMMA}/events",
                params={
                    "series_slug": SERIES_SLUG,
                    "closed": closed,
                    "limit": 50,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "false",
                },
                timeout=30,
            )
            if sleep_s:
                time.sleep(sleep_s)
            if resp.status_code != 200:
                print(f"WARN gamma {resp.status_code} offset={offset}", file=sys.stderr)
                break
            events = resp.json() or []
            if not events:
                break
            oldest = None
            for event in events:
                if not isinstance(event, dict):
                    continue
                for raw in event.get("markets") or []:
                    if not isinstance(raw, dict):
                        continue
                    m = market_from_gamma(raw, event)
                    if m is None:
                        continue
                    oldest = m.end_ts if oldest is None else min(oldest, m.end_ts)
                    if m.end_ts < start_ts or m.end_ts > end_ts:
                        continue
                    out[m.condition_id] = m
            offset += len(events)
            if offset % 500 == 0:
                print(f"  gamma offset={offset} kept={len(out)} oldest={oldest}")
            if oldest is not None and oldest < start_ts:
                break
            if len(events) < 50:
                break
    return sorted(out.values(), key=lambda m: m.end_ts)


def candidate_unix_slugs(start_unix: int) -> List[str]:
    return [f"{pfx}{start_unix}" for pfx in SLUG_PREFIXES]


def fetch_market_by_unix(
    http: requests.Session,
    start_unix: int,
    sleep_s: float,
) -> Optional[MarketMeta]:
    for slug in candidate_unix_slugs(start_unix):
        resp = http.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20)
        if sleep_s:
            time.sleep(sleep_s)
        if resp.status_code != 200:
            continue
        events = resp.json() or []
        if not events or not isinstance(events[0], dict):
            # Also try markets endpoint
            resp2 = http.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=20)
            if sleep_s:
                time.sleep(sleep_s)
            if resp2.status_code == 200:
                raws = resp2.json() or []
                if isinstance(raws, list) and raws and isinstance(raws[0], dict):
                    m = market_from_gamma(raws[0], raws[0])
                    if m is not None:
                        return m
            continue
        event = events[0]
        for raw in event.get("markets") or []:
            if not isinstance(raw, dict):
                continue
            m = market_from_gamma(raw, event)
            if m is not None:
                return m
    return None


def fill_window_via_unix_slugs(
    http: requests.Session,
    w: dict,
    existing: Sequence[MarketMeta],
    sleep_s: float,
    expected_per_day: int = 96,
) -> List[MarketMeta]:
    """Backfill 15m slots missing from Gamma series pagination via unix slugs."""
    have = {m.condition_id for m in existing}
    have_starts = {int(m.start_ts) for m in existing}
    out = list(existing)
    lo, hi = window_ts_range(w)
    d0 = datetime.fromisoformat(w["start"]).date()
    d1 = datetime.fromisoformat(w["end"]).date()
    # Align to 15m boundaries in UTC (markets keyed by unix start)
    cur_day = d0
    added = 0
    misses = 0
    checked = 0
    while cur_day <= d1:
        day_start = datetime(cur_day.year, cur_day.month, cur_day.day, 0, 0, tzinfo=ET)
        day_end = day_start + timedelta(days=1)
        t0 = int(day_start.timestamp())
        t1 = int(day_end.timestamp())
        # Snap to 900s grid
        t = t0 - (t0 % DURATION_S)
        while t < t1:
            end_t = t + DURATION_S
            if end_t < lo or end_t > hi:
                t += DURATION_S
                continue
            if t in have_starts:
                t += DURATION_S
                continue
            checked += 1
            m = fetch_market_by_unix(http, t, sleep_s)
            if m is None:
                misses += 1
                t += DURATION_S
                continue
            if m.condition_id in have:
                have_starts.add(int(m.start_ts))
                t += DURATION_S
                continue
            if not (lo <= m.end_ts <= hi):
                t += DURATION_S
                continue
            have.add(m.condition_id)
            have_starts.add(int(m.start_ts))
            out.append(m)
            added += 1
            t += DURATION_S
        cur_day += timedelta(days=1)
    out.sort(key=lambda m: m.end_ts)
    print(
        f"  unix-fill {w['id']}: +{added} misses={misses} checked={checked} "
        f"(now {len(out)}; expect~{expected_per_day}*(days))"
    )
    return out


def load_or_build_index(
    http: requests.Session,
    windows: Sequence[dict],
    rebuild: bool,
    sleep_s: float,
) -> List[MarketMeta]:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    ranges = [window_ts_range(w) for w in windows]
    need_lo = min(a for a, _ in ranges) - DURATION_S
    need_hi = max(b for _, b in ranges) + DURATION_S

    if INDEX_PATH.exists() and not rebuild:
        raw = json.loads(INDEX_PATH.read_text())
        markets = [MarketMeta(**row) for row in raw]
        if markets:
            print(
                f"index cache hit: {len(markets)} markets ({INDEX_PATH}); "
                "sparse windows will unix-fill"
            )
            return markets
        print("index cache empty — rebuilding")

    print(
        "building market index "
        f"{datetime.fromtimestamp(need_lo, tz=timezone.utc)} .. "
        f"{datetime.fromtimestamp(need_hi, tz=timezone.utc)} UTC"
    )
    markets = build_market_index(http, need_lo, need_hi, sleep_s)
    INDEX_PATH.write_text(
        json.dumps([asdict(m) for m in markets], separators=(",", ":"))
    )
    print(
        f"wrote {len(markets)} markets -> {INDEX_PATH} "
        f"({INDEX_PATH.stat().st_size} bytes)"
    )
    return markets


def prices_history(
    http: requests.Session,
    token_id: str,
    start_ts: float,
    end_ts: float,
    fidelity: int,
    sleep_s: float,
    use_cache: bool,
) -> List[Tuple[float, float]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = f"{token_id}_{int(start_ts)}_{int(end_ts)}_{fidelity}.json"
    cache = CACHE_DIR / key
    if use_cache and cache.exists():
        try:
            data = json.loads(cache.read_text())
            return [(float(t), float(p)) for t, p in data]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    resp = http.get(
        f"{CLOB}/prices-history",
        params={
            "market": token_id,
            "startTs": int(start_ts),
            "endTs": int(end_ts),
            "fidelity": fidelity,
        },
        timeout=25,
    )
    if sleep_s:
        time.sleep(sleep_s)
    if resp.status_code != 200:
        return []
    payload = resp.json()
    hist = payload.get("history") if isinstance(payload, dict) else payload
    out: List[Tuple[float, float]] = []
    for pt in hist or []:
        try:
            out.append((float(pt["t"]), float(pt["p"])))
        except (TypeError, ValueError, KeyError):
            continue
    if use_cache and out:
        cache.write_text(
            json.dumps([[int(t), round(p, 6)] for t, p in out], separators=(",", ":"))
        )
    return out



def fetch_trades(
    http: requests.Session,
    condition_id: str,
    start_ts: float,
    end_ts: float,
    sleep_s: float,
    use_cache: bool,
) -> List[dict]:
    """Public Data API last-trades for one condition (same pattern as 92c)."""
    TRADES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    start_i, end_i = int(start_ts), int(end_ts)
    cache = TRADES_CACHE_DIR / f"{condition_id}_{start_i}_{end_i}.json"
    if use_cache and cache.exists():
        try:
            return json.loads(cache.read_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    rows: List[dict] = []
    offset = 0
    while offset <= 20_000:
        resp = http.get(
            TRADES_URL,
            params={
                "market": condition_id,
                "start": start_i,
                "end": end_i,
                "limit": 1000,
                "offset": offset,
            },
            timeout=30,
        )
        if sleep_s:
            time.sleep(sleep_s)
        if resp.status_code != 200:
            break
        batch = resp.json() or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    slim = []
    for r in rows:
        try:
            ts = float(r["timestamp"])
            px = float(r["price"])
        except (TypeError, ValueError, KeyError):
            continue
        slim.append(
            {
                "ts": int(ts),
                "px": px,
                "outcome": str(r.get("outcome") or "").lower(),
            }
        )
    slim.sort(key=lambda item: item["ts"])
    if use_cache and slim:
        cache.write_text(json.dumps(slim, separators=(",", ":")))
    return slim


def paths_from_trades(
    trades: Sequence[dict],
    start_ts: float,
    end_ts: float,
) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """Last print per unix-second per side -> up/dn (t, p) paths.

    Empirical BTC 15m last-8m tape: median unique-second gaps ≈2s (p90 ≈5–9s).
    That is denser than CLOB prices-history ≈60s and honest vs claiming 15s CLOB.
    """
    by_sec_up: Dict[int, float] = {}
    by_sec_dn: Dict[int, float] = {}
    t0, t1 = int(start_ts), int(end_ts)
    for row in trades:
        try:
            sec = int(row["ts"])
            px = float(row["px"])
            outcome = str(row.get("outcome") or "").lower()
        except (TypeError, ValueError, KeyError):
            continue
        if sec < t0 or sec > t1:
            continue
        if outcome == "up":
            by_sec_up[sec] = px
        elif outcome == "down":
            by_sec_dn[sec] = px
    up = [(float(s), by_sec_up[s]) for s in sorted(by_sec_up)]
    dn = [(float(s), by_sec_dn[s]) for s in sorted(by_sec_dn)]
    return up, dn


@dataclass
class Entry:
    window_id: str
    buy_window_s: int
    slug: str
    end_ts: float
    band: str
    side: str
    entry_ts: float
    entry_px: float
    winner: Optional[str]
    win: Optional[bool]
    opp_min_after: Optional[float]
    hedge_opp_le35: Optional[bool]
    hedge_opp_le40: Optional[bool]
    hedge_opp_le60: Optional[bool]
    ttm_at_entry: Optional[float]


def first_touch(
    up: Sequence[Tuple[float, float]],
    dn: Sequence[Tuple[float, float]],
    lo: float,
    hi: float,
) -> Optional[Tuple[str, float, float]]:
    merged: List[Tuple[float, str, float]] = []
    for t, p in up:
        if lo - 1e-12 <= p <= hi + 1e-12:
            merged.append((t, "up", p))
    for t, p in dn:
        if lo - 1e-12 <= p <= hi + 1e-12:
            merged.append((t, "down", p))
    if not merged:
        return None
    merged.sort(key=lambda x: x[0])
    t, side, p = merged[0]
    return side, t, p


def opp_path_stats(
    entry_side: str,
    entry_ts: float,
    up: Sequence[Tuple[float, float]],
    dn: Sequence[Tuple[float, float]],
) -> Tuple[Optional[float], Dict[float, Optional[bool]]]:
    series = dn if entry_side == "up" else up
    after = [p for t, p in series if t >= entry_ts - 1]
    if not after:
        return None, {th: None for th in HEDGE_THRESHOLDS}
    mn = min(after)
    flags = {th: (mn <= th + 1e-12) for th in HEDGE_THRESHOLDS}
    return mn, flags


def analyze_market(
    http: requests.Session,
    m: MarketMeta,
    window_id: str,
    buy_window_s: int,
    fidelity: int,
    sleep_s: float,
    use_cache: bool,
) -> List[Entry]:
    w0 = max(m.start_ts, m.end_ts - buy_window_s)
    w1 = m.end_ts
    # Fetch full last-max-window once if caller sweeps multiple buy windows —
    # here we fetch per buy_window (cache makes repeats cheap).
    up = prices_history(http, m.up_token, w0, w1, fidelity, sleep_s, use_cache)
    dn = prices_history(http, m.dn_token, w0, w1, fidelity, sleep_s, use_cache)
    out: List[Entry] = []

    def build(band: str, touch: Optional[Tuple[str, float, float]]) -> Optional[Entry]:
        if touch is None:
            return None
        side, ts, px = touch
        opp_min, hedges = opp_path_stats(side, ts, up, dn)
        win = None if m.winner is None else (side == m.winner)
        return Entry(
            window_id=window_id,
            buy_window_s=buy_window_s,
            slug=m.slug,
            end_ts=m.end_ts,
            band=band,
            side=side,
            entry_ts=ts,
            entry_px=px,
            winner=m.winner,
            win=win,
            opp_min_after=opp_min,
            hedge_opp_le35=hedges[0.35],
            hedge_opp_le40=hedges[0.40],
            hedge_opp_le60=hedges[0.60],
            ttm_at_entry=m.end_ts - ts,
        )

    e95 = build("ge95", first_touch(up, dn, HI_95, 1.0))
    if e95:
        out.append(e95)
    e90 = build("90_94", first_touch(up, dn, LO_90, HI_90))
    if e90:
        out.append(e90)
    return out


def analyze_market_multi(
    http: requests.Session,
    m: MarketMeta,
    window_id: str,
    buy_windows: Sequence[int],
    fidelity: int,
    sleep_s: float,
    use_cache: bool,
    source: str = "trades",
) -> List[Entry]:
    """Fetch longest buy window once, then slice for each WINDOW_S."""
    max_w = max(buy_windows)
    w0 = max(m.start_ts, m.end_ts - max_w)
    w1 = m.end_ts
    if source == "trades":
        trades = fetch_trades(
            http, m.condition_id, w0, w1 + 5, sleep_s, use_cache
        )
        up_full, dn_full = paths_from_trades(trades, w0, w1)
    else:
        up_full = prices_history(
            http, m.up_token, w0, w1, fidelity, sleep_s, use_cache
        )
        dn_full = prices_history(
            http, m.dn_token, w0, w1, fidelity, sleep_s, use_cache
        )
    out: List[Entry] = []
    for bw in buy_windows:
        cut = m.end_ts - bw
        up = [(t, p) for t, p in up_full if t >= cut - 1]
        dn = [(t, p) for t, p in dn_full if t >= cut - 1]

        def build(
            band: str,
            touch: Optional[Tuple[str, float, float]],
            bw_local: int = bw,
            up_l: Sequence[Tuple[float, float]] = up,
            dn_l: Sequence[Tuple[float, float]] = dn,
        ) -> Optional[Entry]:
            if touch is None:
                return None
            side, ts, px = touch
            opp_min, hedges = opp_path_stats(side, ts, up_l, dn_l)
            win = None if m.winner is None else (side == m.winner)
            return Entry(
                window_id=window_id,
                buy_window_s=bw_local,
                slug=m.slug,
                end_ts=m.end_ts,
                band=band,
                side=side,
                entry_ts=ts,
                entry_px=px,
                winner=m.winner,
                win=win,
                opp_min_after=opp_min,
                hedge_opp_le35=hedges[0.35],
                hedge_opp_le40=hedges[0.40],
                hedge_opp_le60=hedges[0.60],
                ttm_at_entry=m.end_ts - ts,
            )

        e95 = build("ge95", first_touch(up, dn, HI_95, 1.0))
        if e95:
            out.append(e95)
        e90 = build("90_94", first_touch(up, dn, LO_90, HI_90))
        if e90:
            out.append(e90)
    return out


def fetch_binance_daily(
    http: requests.Session, start_day: str, end_day: str
) -> Dict[str, dict]:
    start = datetime.fromisoformat(start_day).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_day).replace(tzinfo=timezone.utc) + timedelta(days=2)
    resp = http.get(
        BINANCE,
        params={
            "symbol": "BTCUSDT",
            "interval": "1d",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000),
            "limit": 1000,
        },
        timeout=30,
    )
    out: Dict[str, dict] = {}
    if resp.status_code != 200:
        return out
    for k in resp.json() or []:
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        day = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        out[day] = {
            "ret_pct": (c - o) / o * 100.0 if o else 0.0,
            "range_pct": (h - l) / o * 100.0 if o else 0.0,
            "close": c,
        }
    return out


def summarize_entries(entries: Sequence[Entry]) -> dict:
    decided = [e for e in entries if e.win is not None]
    wins = sum(1 for e in decided if e.win)
    n = len(decided)
    losses = [e for e in decided if not e.win]
    mean_px = statistics.mean([e.entry_px for e in decided]) if decided else None

    def loss_hedge_rate(attr: str) -> Optional[float]:
        if not losses:
            return None
        hit = sum(1 for e in losses if getattr(e, attr))
        return hit / len(losses)

    def all_hedge_rate(attr: str) -> Optional[float]:
        if not n:
            return None
        return sum(1 for e in decided if getattr(e, attr)) / n

    mean_ttm = (
        statistics.mean([e.ttm_at_entry for e in decided if e.ttm_at_entry is not None])
        if any(e.ttm_at_entry is not None for e in decided)
        else None
    )
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "wr": (wins / n) if n else None,
        "mean_entry": mean_px,
        "mean_ttm_at_entry": mean_ttm,
        "hedge_opp_rate_all_le35": all_hedge_rate("hedge_opp_le35"),
        "hedge_opp_rate_losses_le35": loss_hedge_rate("hedge_opp_le35"),
        "hedge_opp_rate_all_le40": all_hedge_rate("hedge_opp_le40"),
        "hedge_opp_rate_losses_le40": loss_hedge_rate("hedge_opp_le40"),
        "hedge_opp_rate_all_le60": all_hedge_rate("hedge_opp_le60"),
        "hedge_opp_rate_losses_le60": loss_hedge_rate("hedge_opp_le60"),
        "unresolved": sum(1 for e in entries if e.win is None),
    }


def ev_sketch(
    wr: Optional[float],
    mean_entry: Optional[float],
    notional: float,
    h_scratch: float = 0.45,
) -> Optional[dict]:
    """Hourly-style EV: shares×(1−h)×(WR−BE) with BE≈mean_entry, h≈scratch dump.

    shares = notional / mean_entry. Perfect-hold EV = shares*(WR*1 + (1-WR)*0 - mean_entry)
    = shares*(WR - mean_entry). With hedge scratch fraction h of losers rescued near 0:
    approximate edge retained on non-scratched mass: (1-h)*(WR - BE).
    """
    if wr is None or mean_entry is None or mean_entry <= 0:
        return None
    shares = notional / mean_entry
    be = mean_entry
    hold_ev = shares * (wr - be)
    scratch_ev = shares * (1.0 - h_scratch) * (wr - be)
    return {
        "notional": notional,
        "shares": shares,
        "be": be,
        "wr": wr,
        "h_scratch": h_scratch,
        "hold_ev_per_trade": hold_ev,
        "scratch_ev_per_trade": scratch_ev,
    }


def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * x:.1f}%"


def fmt_px(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x:.4f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--windows", default="", help="Comma ids e.g. W06,W11 (default all)")
    ap.add_argument(
        "--buy-windows",
        default="180,300,480",
        help="Comma buy WINDOW_S seconds (default 180,300,480)",
    )
    ap.add_argument("--rebuild-index", action="store_true")
    ap.add_argument("--fidelity", type=int, default=1, help="CLOB fidelity minutes (1≈1m)")
    ap.add_argument(
        "--source",
        choices=("clob", "trades"),
        default="trades",
        help=(
            "trades=Data API last-prints (~2s; DEFAULT/canonical denser tape); "
            "clob=/prices-history (~1m floor)"
        ),
    )
    ap.add_argument(
        "--export-tag",
        default="",
        help=(
            "Optional suffix for export filenames. Empty + default trades -> "
            "main panel (canonical). Empty + clob auto-tags clob."
        ),
    )
    ap.add_argument("--sleep", type=float, default=0.04)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--clean-cache", action="store_true")
    ap.add_argument(
        "--skip-unix-fill",
        action="store_true",
        help="Do not backfill sparse windows via unix slugs",
    )
    ap.add_argument(
        "--min-fill-threshold",
        type=int,
        default=40,
        help="Unix-fill if window has fewer than this many markets",
    )
    args = ap.parse_args()

    wanted = {x.strip() for x in args.windows.split(",") if x.strip()}
    windows = [w for w in WINDOWS if not wanted or w["id"] in wanted]
    if not windows:
        print("no windows selected", file=sys.stderr)
        return 2

    buy_windows = sorted(
        {int(x.strip()) for x in args.buy_windows.split(",") if x.strip()}
    )
    if not buy_windows:
        print("no buy windows", file=sys.stderr)
        return 2

    http = session()
    markets = load_or_build_index(http, windows, args.rebuild_index, args.sleep)
    by_window: Dict[str, List[MarketMeta]] = {w["id"]: [] for w in windows}
    for m in markets:
        for w in windows:
            lo, hi = window_ts_range(w)
            if lo <= m.end_ts <= hi:
                by_window[w["id"]].append(m)

    if not args.skip_unix_fill:
        for w in windows:
            # Expect ~96 markets/day; fill if sparse relative to day count
            d0 = datetime.fromisoformat(w["start"]).date()
            d1 = datetime.fromisoformat(w["end"]).date()
            n_days = (d1 - d0).days + 1
            expect = n_days * 90  # slight under 96
            thresh = min(args.min_fill_threshold, expect // 2)
            if len(by_window[w["id"]]) < max(thresh, 12):
                by_window[w["id"]] = fill_window_via_unix_slugs(
                    http, w, by_window[w["id"]], args.sleep
                )

    merged: Dict[str, MarketMeta] = {m.condition_id: m for m in markets}
    for ms in by_window.values():
        for m in ms:
            merged[m.condition_id] = m
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(
            [asdict(m) for m in sorted(merged.values(), key=lambda x: x.end_ts)],
            separators=(",", ":"),
        )
    )
    print(f"merged index size={len(merged)} -> {INDEX_PATH}")

    bnc = fetch_binance_daily(http, windows[0]["start"], windows[-1]["end"])

    all_entries: List[Entry] = []
    rows_summary: List[dict] = []

    for w in windows:
        ms = by_window[w["id"]]
        print(
            f"\n=== {w['id']} {w['start']}..{w['end']} {w['regime']} "
            f"markets={len(ms)} buy_windows={buy_windows} ==="
        )
        entries: List[Entry] = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futs = {
                pool.submit(
                    analyze_market_multi,
                    session(),
                    m,
                    w["id"],
                    buy_windows,
                    args.fidelity,
                    args.sleep,
                    not args.no_cache,
                    args.source,
                ): m
                for m in ms
            }
            done = 0
            for fut in as_completed(futs):
                done += 1
                try:
                    entries.extend(fut.result())
                except Exception as exc:  # noqa: BLE001
                    print(f"  WARN {futs[fut].slug}: {exc}", file=sys.stderr)
                if done % 50 == 0 or done == len(ms):
                    print(f"  progress {done}/{len(ms)}")

        all_entries.extend(entries)

        days = []
        d0 = datetime.fromisoformat(w["start"]).date()
        d1 = datetime.fromisoformat(w["end"]).date()
        cur = d0
        while cur <= d1:
            days.append(cur.isoformat())
            cur += timedelta(days=1)
        rets = [bnc[d]["ret_pct"] for d in days if d in bnc]
        rngs = [bnc[d]["range_pct"] for d in days if d in bnc]
        bnc_note = (
            f"btc_ret_sum={sum(rets):+.2f}% mean_range={statistics.mean(rngs):.2f}%"
            if rets and rngs
            else "btc_n/a"
        )

        for bw in buy_windows:
            e95 = [e for e in entries if e.band == "ge95" and e.buy_window_s == bw]
            e90 = [e for e in entries if e.band == "90_94" and e.buy_window_s == bw]
            s95 = summarize_entries(e95)
            s90 = summarize_entries(e90)
            row = {
                "window": w["id"],
                "buy_window_s": bw,
                "dates": f"{w['start']}..{w['end']}",
                "regime": w["regime"],
                "dir": w["dir"],
                "vol": w["vol"],
                "markets": len(ms),
                "n_ge95": s95["n"],
                "wr_ge95": s95["wr"],
                "wins_ge95": s95["wins"],
                "mean_entry_ge95": s95["mean_entry"],
                "hedge_loss_ge95_le35": s95["hedge_opp_rate_losses_le35"],
                "hedge_loss_ge95_le40": s95["hedge_opp_rate_losses_le40"],
                "hedge_loss_ge95_le60": s95["hedge_opp_rate_losses_le60"],
                "n_90_94": s90["n"],
                "wr_90_94": s90["wr"],
                "mean_entry_90_94": s90["mean_entry"],
                "hedge_loss_90_94_le35": s90["hedge_opp_rate_losses_le35"],
                "hedge_loss_90_94_le40": s90["hedge_opp_rate_losses_le40"],
                "hedge_loss_90_94_le60": s90["hedge_opp_rate_losses_le60"],
                "btc": bnc_note,
                "notes": w["notes"],
            }
            rows_summary.append(row)
            print(
                f"  bw={bw}s >=95: n={s95['n']} WR={fmt_pct(s95['wr'])} "
                f"mean={fmt_px(s95['mean_entry'])} "
                f"hedge@loss 35/40/60="
                f"{fmt_pct(s95['hedge_opp_rate_losses_le35'])}/"
                f"{fmt_pct(s95['hedge_opp_rate_losses_le40'])}/"
                f"{fmt_pct(s95['hedge_opp_rate_losses_le60'])}"
            )
            print(
                f"  bw={bw}s 90-94.9: n={s90['n']} WR={fmt_pct(s90['wr'])} "
                f"mean={fmt_px(s90['mean_entry'])}"
            )
        print(f"  {bnc_note}")

    # Pooled by buy_window
    pooled_by_bw: Dict[str, dict] = {}
    recent_ids = {"W12"}
    older_ids = {w["id"] for w in windows} - recent_ids
    for bw in buy_windows:
        e95 = [e for e in all_entries if e.band == "ge95" and e.buy_window_s == bw]
        e90 = [e for e in all_entries if e.band == "90_94" and e.buy_window_s == bw]
        pooled_by_bw[str(bw)] = {
            "all_ge95": summarize_entries(e95),
            "recent_W12_ge95": summarize_entries(
                [e for e in e95 if e.window_id in recent_ids]
            ),
            "older_excl_W12_ge95": summarize_entries(
                [e for e in e95 if e.window_id in older_ids]
            ),
            "all_90_94": summarize_entries(e90),
            "up_day_ge95": summarize_entries(
                [
                    e
                    for e in e95
                    if next((x for x in windows if x["id"] == e.window_id), {}).get(
                        "dir"
                    )
                    == "up"
                    or next((x for x in windows if x["id"] == e.window_id), {}).get(
                        "dir"
                    )
                    == "upish"
                ]
            ),
            "down_day_ge95": summarize_entries(
                [
                    e
                    for e in e95
                    if next((x for x in windows if x["id"] == e.window_id), {}).get(
                        "dir"
                    )
                    == "down"
                ]
            ),
        }

    # Dir/vol buckets for primary buy window (300s if present else first)
    primary_bw = 300 if 300 in buy_windows else buy_windows[0]

    def bucket(rows: Sequence[dict], key: str, bw: int) -> List[dict]:
        groups: Dict[str, List[dict]] = {}
        for r in rows:
            if int(r["buy_window_s"]) != bw:
                continue
            groups.setdefault(str(r[key]), []).append(r)
        out = []
        for k, rs in sorted(groups.items()):
            ids = {r["window"] for r in rs}
            ents = [
                e
                for e in all_entries
                if e.window_id in ids
                and e.band == "ge95"
                and e.buy_window_s == bw
            ]
            s = summarize_entries(ents)
            out.append({"bucket": k, **s, "windows": sorted(ids)})
        return out

    buckets_dir = bucket(rows_summary, "dir", primary_bw)
    buckets_vol = bucket(rows_summary, "vol", primary_bw)
    buckets_reg = bucket(rows_summary, "regime", primary_bw)

    # Pick best-looking for EV sketch: highest WR among n>=30 ge95 pools
    best = None
    for bw in buy_windows:
        for band, key in (("ge95", "all_ge95"), ("90_94", "all_90_94")):
            s = pooled_by_bw[str(bw)][key]
            if s["n"] < 20 or s["wr"] is None:
                continue
            score = (s["wr"] or 0) - (s["mean_entry"] or 1)
            cand = {
                "buy_window_s": bw,
                "band": band,
                "summary": s,
                "edge": score,
            }
            if best is None or score > best["edge"]:
                best = cand

    ev_sketches = {}
    if best:
        s = best["summary"]
        for notion in (2.0, 20.0):
            ev_sketches[f"${notion:g}"] = ev_sketch(
                s["wr"], s["mean_entry"], notion, h_scratch=0.45
            )

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tag = (args.export_tag or "").strip().strip("_")
    # Default source is trades (canonical denser). Untagged exports are the
    # main panel. Auto-tag only when using the alternate clob source.
    if not tag and args.source != "trades":
        tag = args.source
    stem = f"15m_regime_panel_{tag}" if tag else "15m_regime_panel"
    summary_csv = EXPORT_DIR / f"{stem}_summary.csv"
    entries_csv = EXPORT_DIR / f"{stem}_entries.csv"
    report_json = EXPORT_DIR / f"{stem}_report.json"

    with summary_csv.open("w", newline="") as f:
        fields = list(rows_summary[0].keys()) if rows_summary else []
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows_summary:
            writer.writerow(row)

    with entries_csv.open("w", newline="") as f:
        fields = [
            "window_id",
            "buy_window_s",
            "slug",
            "end_ts",
            "band",
            "side",
            "entry_ts",
            "entry_px",
            "winner",
            "win",
            "opp_min_after",
            "hedge_opp_le35",
            "hedge_opp_le40",
            "hedge_opp_le60",
            "ttm_at_entry",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for e in all_entries:
            writer.writerow(asdict(e))

    report = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "rule": {
            "series": SERIES_SLUG,
            "duration_s": DURATION_S,
            "buy_windows_s": buy_windows,
            "primary": (
                "first mid/last-print >= 0.95 in last WINDOW_S, hold-to-resolve"
            ),
            "side_panel": "first mid/last-print in [0.90, 0.949] in last WINDOW_S",
            "source": args.source,
            "price_source": (
                (
                    "Data API /trades last-print per unix-second per side "
                    "(~2s median unique-sec gap on BTC 15m; denser than CLOB 1m; "
                    "not TOB mid/ask; no depth)"
                )
                if args.source == "trades"
                else (
                    f"CLOB /prices-history fidelity={args.fidelity}m "
                    "(last-trade proxy ≈1m floor; mid≠ask; no depth; "
                    "sub-minute fidelity NOT available from this endpoint)"
                )
            ),
            "resolution": "Chainlink TWAP (not Binance candle); WR from market outcome",
            "hedge_opp": "after entry, opposite token min <= {0.35, 0.40, 0.60}",
            "pathlog": "not used as primary",
            "export_tag": tag or None,
        },
        "caveats": (
            [
                "last-trade print ≠ ask; fills worse than print",
                "no order-book depth / size at level",
                "Chainlink TWAP resolve — Binance path is regime label only",
                "pathlog not primary tape",
                "trades tape ~1–3s median gaps (not true 15s CLOB candles)",
                "sparse seconds skipped until a print exists on that side",
            ]
            if args.source == "trades"
            else [
                "mid (prices-history) ≠ ask; fills worse than mid",
                "no order-book depth / size at level",
                "Chainlink TWAP resolve — Binance path is regime label only",
                "pathlog not primary tape",
                f"fidelity={args.fidelity} ≈ {args.fidelity}m coarseness "
                "(CLOB does not expose denser history)",
            ]
        ),
        "index_size": len(merged),
        "windows": rows_summary,
        "buckets_dir_primary_bw": buckets_dir,
        "buckets_vol_primary_bw": buckets_vol,
        "buckets_regime_primary_bw": buckets_reg,
        "primary_bw_for_buckets": primary_bw,
        "pooled_by_buy_window": pooled_by_bw,
        "best_looking": best,
        "ev_sketch": ev_sketches,
        "paths": {
            "summary_csv": str(summary_csv),
            "entries_csv": str(entries_csv),
            "report_json": str(report_json),
            "market_index": str(INDEX_PATH),
        },
    }
    report_json.write_text(json.dumps(report, indent=2, default=str))

    print("\n" + "=" * 78)
    print("POOLED by buy-window (>=95c first touch, hold-to-resolve)")
    print("=" * 78)
    for bw in buy_windows:
        p = pooled_by_bw[str(bw)]
        a = p["all_ge95"]
        r = p["recent_W12_ge95"]
        o = p["older_excl_W12_ge95"]
        n90 = p["all_90_94"]
        print(
            f"  WINDOW={bw}s  >=95: n={a['n']} WR={fmt_pct(a['wr'])} "
            f"mean={fmt_px(a['mean_entry'])}  "
            f"hedge@loss 35/40/60="
            f"{fmt_pct(a['hedge_opp_rate_losses_le35'])}/"
            f"{fmt_pct(a['hedge_opp_rate_losses_le40'])}/"
            f"{fmt_pct(a['hedge_opp_rate_losses_le60'])}"
        )
        print(
            f"             90-94.9: n={n90['n']} WR={fmt_pct(n90['wr'])} "
            f"mean={fmt_px(n90['mean_entry'])}"
        )
        print(
            f"             recent W12: n={r['n']} WR={fmt_pct(r['wr'])} | "
            f"older: n={o['n']} WR={fmt_pct(o['wr'])}"
        )
        print(
            f"             up/upish: n={p['up_day_ge95']['n']} "
            f"WR={fmt_pct(p['up_day_ge95']['wr'])} | "
            f"down: n={p['down_day_ge95']['n']} "
            f"WR={fmt_pct(p['down_day_ge95']['wr'])}"
        )

    if best:
        print("\nBEST-LOOKING (pooled edge WR-mean_entry)")
        print(
            f"  buy_window={best['buy_window_s']}s band={best['band']} "
            f"n={best['summary']['n']} WR={fmt_pct(best['summary']['wr'])} "
            f"mean={fmt_px(best['summary']['mean_entry'])} edge={best['edge']:.4f}"
        )
        for k, v in ev_sketches.items():
            if v:
                print(
                    f"  EV {k}: shares={v['shares']:.2f} hold_ev/trade="
                    f"{v['hold_ev_per_trade']:+.4f} "
                    f"scratch_h={v['h_scratch']} ev/trade="
                    f"{v['scratch_ev_per_trade']:+.4f}"
                )

    print("\nWrote:")
    print(f"  {summary_csv}")
    print(f"  {entries_csv}")
    print(f"  {report_json}")
    print(f"  {INDEX_PATH} (n={len(merged)})")

    if args.clean_cache:
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            print(f"cleaned {CACHE_DIR}")
        if TRADES_CACHE_DIR.exists():
            shutil.rmtree(TRADES_CACHE_DIR, ignore_errors=True)
            print(f"cleaned {TRADES_CACHE_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
