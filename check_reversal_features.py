#!/usr/bin/env python3
"""Correlate BTC level, short-horizon vol, and momentum with 5m reversals.

No orders. No .env. Binance BTCUSDT is a proxy for Chainlink TWAP 30s (the
live 5m oracle). Pathlog ~1s books are not required.

Session tape (operator UI export):

    python check_reversal_features.py --csv history.csv \\
        --restart-utc 2026-08-27T08:57:16

Historical 5m windows (public Binance, optional CLOB last-trades):

    python check_reversal_features.py --hours 72
    python check_reversal_features.py --hours 168 --binance-interval 1m
    python check_reversal_features.py --hours 336 --binance-interval 1m
    python check_reversal_features.py --hours 48 --with-clob

A reversal here is: the BTC side of PTB at sample time disagrees with the
side at the window close. CLOB mode additionally requires a 75–90¢ last
print in the last 120s (the live late band) before scoring that market.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from check_hedge_threshold import (
    fetch_event,
    fetch_trades,
    five_m_start_ts,
    load_csv,
    series_of,
    session as hedge_session,
)

# api.binance.com is geo-blocked on some cloud egress; the vision mirror is public.
BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines"
USER_AGENT = "Mozilla/5.0 (compatible; poly-money-maker-reversal-research/1.0)"
SLUG_PREFIX = "btc-updown-5m"
WINDOW_S = 300
LATE_TTM_S = 120.0
LATE_MIN = 0.75
LATE_MAX = 0.90
BUDGET = 2.50
DEFAULT_FILL_PX = 0.85

# |BTC−PTB| buckets in USD (Binance proxy).
DIST_EDGES = (0.0, 5.0, 10.0, 20.0, 40.0, 80.0, math.inf)
FINE_DIST_EDGES = tuple(float(x) for x in range(0, 85, 5)) + (math.inf,)
GATE_MINS = (0, 10, 15, 20, 25, 30, 35, 40, 50, 60, 80)
# Typical salvage on a dumped loser from the 27 Aug session (~$1 on ~$2.50).
DEFAULT_SALVAGE = 1.00
# 30s realized move (std of 1s diffs, USD).
VOL_EDGES = (0.0, 2.0, 4.0, 8.0, 16.0, math.inf)
# |dist| / (1s-vol * sqrt(TTM)) — Brownian "how many sigmas to PTB".
Z_EDGES = (0.0, 1.0, 2.0, 4.0, 8.0, math.inf)
# |30s momentum| in USD.
MOM_EDGES = (0.0, 5.0, 10.0, 20.0, 40.0, math.inf)


def http_session() -> requests.Session:
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


def parse_utc(text: str) -> datetime:
    raw = (text or "").strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def side_of(px: Optional[float], ptb: Optional[float]) -> Optional[str]:
    if px is None or ptb is None:
        return None
    delta = float(px) - float(ptb)
    if delta > 0:
        return "up"
    if delta < 0:
        return "down"
    return None


def stdev(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    return statistics.pstdev(values)


def realized_move(prices: Sequence[float]) -> Optional[float]:
    """USD std of successive diffs — typical 1-step move over the sample."""
    if len(prices) < 3:
        return None
    diffs = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    return stdev(diffs)


def seconds_to_cross(dist: float, vel_per_s: float) -> Optional[float]:
    """Linear time to PTB if velocity is toward the open. None = not approaching."""
    if dist == 0:
        return 0.0
    if vel_per_s == 0 or dist * vel_per_s >= 0:
        return None
    return abs(dist) / abs(vel_per_s)


def bucket(value: Optional[float], edges: Sequence[float]) -> str:
    if value is None or not math.isfinite(value):
        return "na"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= value < hi:
            if math.isinf(hi):
                return f">={lo:g}"
            return f"{lo:g}–{hi:g}"
    return f">={edges[-2]:g}"


def mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def money(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:+.2f}"


def pct(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.1%}"


@dataclass
class BtcSeries:
    """Sorted (unix_s, close) samples."""

    ts: list[int]
    px: list[float]

    def _idx_at_or_before(self, unix: float) -> int:
        if not self.ts:
            return -1
        lo, hi = 0, len(self.ts)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.ts[mid] <= unix:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1

    def at_or_before(self, unix: float) -> Optional[float]:
        idx = self._idx_at_or_before(unix)
        if idx < 0:
            return None
        return self.px[idx]

    def sample_at_or_before(self, unix: float) -> Optional[tuple[int, float]]:
        idx = self._idx_at_or_before(unix)
        if idx < 0:
            return None
        return self.ts[idx], self.px[idx]

    def window(self, start: float, end: float) -> list[float]:
        if not self.ts:
            return []
        lo, hi = 0, len(self.ts)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.ts[mid] < start:
                lo = mid + 1
            else:
                hi = mid
        left = lo
        lo, hi = 0, len(self.ts)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.ts[mid] <= end:
                lo = mid + 1
            else:
                hi = mid
        return self.px[left:lo]


def merge_klines(chunks: Iterable[list[tuple[int, float]]]) -> BtcSeries:
    seen: dict[int, float] = {}
    for chunk in chunks:
        for ts, px in chunk:
            seen[int(ts)] = float(px)
    items = sorted(seen.items())
    return BtcSeries(ts=[t for t, _ in items], px=[p for _, p in items])


def fetch_binance_klines(
    http: requests.Session,
    *,
    start_s: int,
    end_s: int,
    interval: str,
    cache_dir: Path,
    pause_s: float = 0.05,
) -> BtcSeries:
    """Inclusive [start_s, end_s] close samples. Caches per 1000-bar page."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    start_ms = int(start_s) * 1000
    end_ms = int(end_s) * 1000
    chunks: list[list[tuple[int, float]]] = []
    cursor = start_ms
    while cursor <= end_ms:
        page = cache_dir / f"bn_{interval}_{cursor}.json"
        raw = None
        if page.exists():
            raw = json.loads(page.read_text())
        else:
            # Reuse complete pages cached under the old endTime-suffixed name.
            for old in sorted(cache_dir.glob(f"bn_{interval}_{cursor}_*.json")):
                prev = json.loads(old.read_text())
                if len(prev) >= 1000:
                    raw = prev
                    page.write_text(json.dumps(prev))
                    break
        if raw is None:
            resp = http.get(
                BINANCE_KLINES,
                params={
                    "symbol": "BTCUSDT",
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
                timeout=30,
            )
            resp.raise_for_status()
            raw = resp.json()
            if len(raw) >= 1000:
                page.write_text(json.dumps(raw))
            time.sleep(pause_s)
        if not raw:
            break
        rows = []
        last_open = None
        for bar in raw:
            open_ms = int(bar[0])
            close = float(bar[4])
            rows.append((open_ms // 1000, close))
            last_open = open_ms
        chunks.append(rows)
        if last_open is None or len(raw) < 1000:
            break
        nxt = last_open + 1
        if nxt <= cursor:
            break
        cursor = nxt
    return merge_klines(chunks)


@dataclass
class Features:
    ts: float
    ttm: float
    px: float
    ptb: float
    dist: float
    abs_dist: float
    side: str
    mom_15s: Optional[float] = None
    mom_30s: Optional[float] = None
    mom_60s: Optional[float] = None
    vol_30s: Optional[float] = None
    vol_60s: Optional[float] = None
    against_30s: Optional[bool] = None
    ripping_to_ptb: Optional[bool] = None
    sec_to_cross: Optional[float] = None
    cross_before_end: Optional[bool] = None
    flip_z: Optional[float] = None
    fill_px: Optional[float] = None
    leg: Optional[str] = None

    def row(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "ttm": self.ttm,
            "px": self.px,
            "ptb": self.ptb,
            "dist": self.dist,
            "abs_dist": self.abs_dist,
            "side": self.side,
            "mom_15s": self.mom_15s,
            "mom_30s": self.mom_30s,
            "mom_60s": self.mom_60s,
            "vol_30s": self.vol_30s,
            "vol_60s": self.vol_60s,
            "against_30s": self.against_30s,
            "ripping_to_ptb": self.ripping_to_ptb,
            "sec_to_cross": self.sec_to_cross,
            "cross_before_end": self.cross_before_end,
            "flip_z": self.flip_z,
            "fill_px": self.fill_px,
            "leg": self.leg,
        }


def features_at(
    btc: BtcSeries,
    *,
    ts: float,
    end_ts: float,
    ptb: float,
    fill_px: Optional[float] = None,
    leg: Optional[str] = None,
) -> Optional[Features]:
    now = btc.sample_at_or_before(ts)
    if now is None:
        return None
    _, px = now
    dist = px - ptb
    if dist == 0:
        return None
    side = "up" if dist > 0 else "down"

    def _mom(lookback: float, min_age: float) -> Optional[float]:
        ago = btc.sample_at_or_before(ts - lookback)
        now_s = btc.sample_at_or_before(ts)
        if ago is None or now_s is None:
            return None
        if ago[0] >= now_s[0]:
            return None
        age = ts - ago[0]
        if age < min_age:
            return None
        return px - ago[1]

    mom15 = _mom(15, 8)
    mom30 = _mom(30, 20)
    mom60 = _mom(60, 40)
    w30 = btc.window(ts - 30, ts)
    w60 = btc.window(ts - 300, ts) if len(w30) < 5 else btc.window(ts - 60, ts)
    vol30 = realized_move(w30)
    vol60 = realized_move(w60)
    mom_dir = mom30 if mom30 is not None else mom60
    mom_span = 30.0 if mom30 is not None else (60.0 if mom60 is not None else None)
    against = None if mom_dir is None else (dist * mom_dir < 0)
    vel = None if mom_dir is None or mom_span is None else mom_dir / mom_span
    stc = None if vel is None else seconds_to_cross(dist, vel)
    ttm = end_ts - ts
    cross = None if stc is None else (stc <= ttm)
    flip_z = None
    if vol30 is not None and ttm > 0:
        flip_z = abs(dist) / (max(vol30, 1e-9) * math.sqrt(ttm))
    return Features(
        ts=ts,
        ttm=ttm,
        px=px,
        ptb=ptb,
        dist=dist,
        abs_dist=abs(dist),
        side=side,
        mom_15s=mom15,
        mom_30s=mom30,
        mom_60s=mom60,
        vol_30s=vol30,
        vol_60s=vol60,
        against_30s=against,
        ripping_to_ptb=against,
        sec_to_cross=stc,
        cross_before_end=cross,
        flip_z=flip_z,
        fill_px=fill_px,
        leg=leg,
    )


@dataclass
class Sample:
    slug: str
    start_ts: int
    end_ts: int
    feat: Features
    close_px: float
    winner: str
    reversed: bool
    soft_close: bool
    source: str
    pnl_redeem: float = 0.0
    outcome: str = ""  # redeem | hedge | unresolved | paper


def paper_redeem_pnl(fill_px: float, won: bool, budget: float = BUDGET) -> float:
    if fill_px <= 0:
        return 0.0
    shares = budget / fill_px
    return (shares - budget) if won else -budget


def breakeven_flip_rate(
    fill_px: float,
    salvage: float = 0.0,
    budget: float = BUDGET,
) -> Optional[float]:
    """Largest flip rate with EV ≥ 0. ``salvage`` is $ recovered on a loser.

    With no hedge, this is ``1 - fill_px`` (an 85¢ fill needs ≤15% flips).
    """
    p = float(fill_px)
    if p <= 0 or budget <= 0:
        return None
    win = budget / p
    denom = win - salvage
    if denom <= 1e-12:
        return 1.0
    wr = (budget - salvage) / denom
    wr = min(1.0, max(0.0, wr))
    return 1.0 - wr


def paper_ev(
    fill_px: float,
    flip_rate: float,
    salvage: float = 0.0,
    budget: float = BUDGET,
) -> Optional[float]:
    """Expected $ at this fill / flip mix (one shot, ``budget`` notional)."""
    p = float(fill_px)
    if p <= 0:
        return None
    wr = 1.0 - float(flip_rate)
    win = budget / p - budget
    lose = salvage - budget
    return wr * win + (1.0 - wr) * lose


def eatable(flip_rate: Optional[float], fill_px: float, salvage: float) -> str:
    cap = breakeven_flip_rate(fill_px, salvage=salvage)
    if flip_rate is None or cap is None:
        return ""
    return "yes" if flip_rate <= cap + 1e-12 else "no"


def implied_fill_px(abs_dist: float) -> float:
    """Map |BTC−PTB| to a 5m ask. Calibrated on the 27 Aug session mix."""
    d = abs(float(abs_dist))
    if d < 15:
        return 0.80
    if d < 25:
        return 0.85
    if d < 40:
        return 0.88
    if d < 80:
        return 0.92
    return 0.94


def paper_fill_pnl(
    fill_px: float,
    won: bool,
    salvage: float = DEFAULT_SALVAGE,
    budget: float = BUDGET,
) -> float:
    if won:
        return paper_redeem_pnl(fill_px, True, budget)
    return float(salvage) - float(budget)


def window_path(
    btc: BtcSeries,
    start_ts: int,
    end_ts: int,
    step: int = 1,
) -> Optional[tuple[float, float, list[tuple[int, float, float, float]]]]:
    """(ptb, close, [(ts, ttm, dist, px), ...]) inside (start, end)."""
    ptb = btc.at_or_before(start_ts)
    close = btc.at_or_before(end_ts)
    if ptb is None or close is None:
        return None
    path: list[tuple[int, float, float, float]] = []
    ts = int(start_ts) + int(step)
    end_i = int(end_ts)
    step = max(1, int(step))
    while ts < end_i:
        px = btc.at_or_before(ts)
        if px is not None:
            path.append((ts, float(end_i - ts), px - ptb, px))
        ts += step
    return ptb, close, path


def first_touch_on_path(
    path: Sequence[tuple[int, float, float, float]],
    *,
    ttm_min: float,
    ttm_max: float,
    min_abs_dist: float,
    max_abs_dist: float = math.inf,
) -> Optional[tuple[int, float, float, float]]:
    """First tick with ttm_min < ttm ≤ ttm_max and min ≤ |dist| < max."""
    for ts, ttm, dist, px in path:
        if ttm <= ttm_min or ttm > ttm_max:
            continue
        ad = abs(dist)
        if ad + 1e-12 < min_abs_dist:
            continue
        if math.isfinite(max_abs_dist) and ad >= max_abs_dist - 1e-12:
            continue
        if dist == 0:
            continue
        return ts, ttm, dist, px
    return None


def first_live_shaped_touch(
    path: Sequence[tuple[int, float, float, float]],
) -> Optional[tuple[int, float, float, float]]:
    """Current live bands via implied fill: early ≥90, late 75–90, last-45 ≥90."""
    for ts, ttm, dist, px in path:
        if dist == 0:
            continue
        fill = implied_fill_px(abs(dist))
        if ttm <= 45 and 0.75 - 1e-12 <= fill <= 0.99 + 1e-12:
            return ts, ttm, dist, px
        if ttm <= 120 and 0.75 - 1e-12 <= fill <= 0.90 + 1e-12:
            return ts, ttm, dist, px
        if 120.0 < ttm <= 300.0 and fill + 1e-12 >= 0.90:
            return ts, ttm, dist, px
    return None


class ComboSpec(NamedTuple):
    name: str
    ttm_min: float
    ttm_max: float
    min_abs_dist: float
    max_abs_dist: float = math.inf


# Named windows, not a cartesian bomb. Grid below covers the rest.
COMBO_SPECS: tuple[ComboSpec, ...] = (
    ComboSpec("last30_e25", 0.0, 30.0, 25.0),
    ComboSpec("last45_e0", 0.0, 45.0, 0.0),
    ComboSpec("last45_e20", 0.0, 45.0, 20.0),
    ComboSpec("last45_e25", 0.0, 45.0, 25.0),
    ComboSpec("last45_e30", 0.0, 45.0, 30.0),
    ComboSpec("last45_e40", 0.0, 45.0, 40.0),
    ComboSpec("late60_e0", 0.0, 60.0, 0.0),
    ComboSpec("late60_e20", 0.0, 60.0, 20.0),
    ComboSpec("late60_e25", 0.0, 60.0, 25.0),
    ComboSpec("late60_e30", 0.0, 60.0, 30.0),
    ComboSpec("late90_e25", 0.0, 90.0, 25.0),
    ComboSpec("late120_e0", 0.0, 120.0, 0.0),
    ComboSpec("late120_e20", 0.0, 120.0, 20.0),
    ComboSpec("late120_e25", 0.0, 120.0, 25.0),
    ComboSpec("late120_e30", 0.0, 120.0, 30.0),
    ComboSpec("late120_e40", 0.0, 120.0, 40.0),
    ComboSpec("live_late_7590", 0.0, 120.0, 15.0, 40.0),
    ComboSpec("early300_e0", 120.0, 300.0, 0.0),
    ComboSpec("early300_e25", 120.0, 300.0, 25.0),
    ComboSpec("union300_e0", 0.0, 300.0, 0.0),
    ComboSpec("union300_e25", 0.0, 300.0, 25.0),
    ComboSpec("union300_e30", 0.0, 300.0, 30.0),
)

GRID_TTM_MAX = (30.0, 45.0, 60.0, 90.0, 120.0)
GRID_EDGE = (0.0, 20.0, 25.0, 30.0, 40.0)

SESSION_REPLAY_GATES: tuple[tuple[str, float, float], ...] = (
    ("last45_e25", 45.0, 25.0),
    ("last45_e20", 45.0, 20.0),
    ("late60_e25", 60.0, 25.0),
    ("late120_e25", 120.0, 25.0),
    ("last45_e0", 45.0, 0.0),
    ("late120_e0", 120.0, 0.0),
)


def _combo_row(
    *,
    name: str,
    hits: int,
    flips: int,
    pnl: float,
    fills: list[float],
    hours: float,
) -> tuple[float, str]:
    if not hits or hours <= 0:
        return float("-inf"), f"{name}\t0\t\t\t\t\t\t\t\t"
    fr = flips / hits
    wr = 1.0 - fr
    mean_f = mean(fills) or DEFAULT_FILL_PX
    per_h = pnl / hours
    eat = eatable(fr, mean_f, 0.0)
    return (
        per_h,
        f"{name}\t{hits}\t{hits / hours:.1f}\t{pct(fr)}\t{pct(wr)}\t"
        f"{mean_f:.2f}\t{money(pnl)}\t{money(per_h)}\t{money(per_h * 2)}\t{eat}",
    )


def score_packed_picks(
    packed: Sequence[tuple[float, float, list[tuple[int, float, float, float]]]],
    pick,
    *,
    name: str,
    hours: float,
    salvage: float,
    budget: float,
) -> tuple[float, str]:
    hits = 0
    flips = 0
    pnl = 0.0
    fills: list[float] = []
    for ptb, close, path in packed:
        hit = pick(path)
        if hit is None:
            continue
        _ts, _ttm, dist, _px = hit
        hits += 1
        fill = implied_fill_px(abs(dist))
        fills.append(fill)
        side = "up" if dist > 0 else "down"
        winner = side_of(close, ptb)
        won = winner == side
        if not won:
            flips += 1
        pnl += paper_fill_pnl(fill, bool(won), salvage=salvage, budget=budget)
    return _combo_row(name=name, hits=hits, flips=flips, pnl=pnl, fills=fills, hours=hours)


def score_combos(
    packed: Sequence[tuple[float, float, list[tuple[int, float, float, float]]]],
    *,
    hours: float,
    salvage: float = DEFAULT_SALVAGE,
    budget: float = BUDGET,
) -> list[str]:
    """First-touch entries. $/hour at ``budget`` and 2× (the $5-size column)."""
    n = len(packed)
    lines = [
        f"COMBOS first-touch  n_windows={n}  hours={hours:.1f}  "
        f"budget=${budget:.2f}  salvage=${salvage:.2f} on losers  "
        f"$5 col = 2× size, same hits",
        "name\thits\thit/hour\tflip\twr\tmean_fill\tpnl\t$/h\t$/h_at_$5\teat_nohedge",
    ]
    rows_out: list[tuple[float, str]] = []
    for spec in COMBO_SPECS:
        rows_out.append(
            score_packed_picks(
                packed,
                lambda path, s=spec: first_touch_on_path(
                    path,
                    ttm_min=s.ttm_min,
                    ttm_max=s.ttm_max,
                    min_abs_dist=s.min_abs_dist,
                    max_abs_dist=s.max_abs_dist,
                ),
                name=spec.name,
                hours=hours,
                salvage=salvage,
                budget=budget,
            )
        )
    rows_out.append(
        score_packed_picks(
            packed,
            first_live_shaped_touch,
            name="live_shaped_union",
            hours=hours,
            salvage=salvage,
            budget=budget,
        )
    )
    rows_out.sort(key=lambda item: item[0], reverse=True)
    lines.extend(row for _score, row in rows_out)
    return lines


def score_grid(
    packed: Sequence[tuple[float, float, list[tuple[int, float, float, float]]]],
    *,
    hours: float,
    salvage: float = DEFAULT_SALVAGE,
    budget: float = BUDGET,
) -> list[str]:
    """Compact TTM × |dist| $/h matrix (the combination picker)."""
    lines = [
        f"GRID $/h at ${budget:.2f}  salvage=${salvage:.2f}  "
        f"cell = $/h (flip%)   first-touch in (0, ttm_max]",
        "edge\\ttm_max\t" + "\t".join(f"{t:.0f}s" for t in GRID_TTM_MAX),
    ]
    for edge in GRID_EDGE:
        cells = [f"e{edge:.0f}"]
        for ttm_max in GRID_TTM_MAX:
            _score, row = score_packed_picks(
                packed,
                lambda path, t=ttm_max, e=edge: first_touch_on_path(
                    path, ttm_min=0.0, ttm_max=t, min_abs_dist=e
                ),
                name="tmp",
                hours=hours,
                salvage=salvage,
                budget=budget,
            )
            parts = row.split("\t")
            # name hits hit/h flip wr mean_fill pnl $/h $/h5 eat
            if len(parts) < 8 or not parts[3]:
                cells.append("—")
                continue
            cells.append(f"{parts[7]} ({parts[3]})")
        lines.append("\t".join(cells))
    return lines


def session_replay_table(samples: Sequence[Sample], hours: float) -> list[str]:
    """Keep session fills whose fill TTM and |dist| pass a combo gate."""
    lines = [
        "SESSION replay — keep actual wallet P&L if fill TTM ≤ window "
        "and |BTC−PTB| ≥ edge (not implied fill)",
        "name\tkeep\tskip\tkeep/h\tpnl_kept\t$/h_kept",
    ]
    base = sum(s.pnl_redeem for s in samples)
    lines.append(
        f"all_fills\t{len(samples)}\t0\t"
        f"{(len(samples) / hours) if hours else 0:.1f}\t"
        f"{money(base)}\t{money(base / hours) if hours else 0}"
    )
    for name, ttm_max, edge in SESSION_REPLAY_GATES:
        kept = [
            s
            for s in samples
            if s.feat.ttm <= ttm_max + 1e-12 and s.feat.abs_dist + 1e-12 >= edge
        ]
        skip_n = len(samples) - len(kept)
        pnl = sum(s.pnl_redeem for s in kept)
        per_h = (pnl / hours) if hours else 0.0
        lines.append(
            f"{name}\t{len(kept)}\t{skip_n}\t"
            f"{(len(kept) / hours) if hours else 0:.1f}\t"
            f"{money(pnl)}\t{money(per_h)}"
        )
    return lines


def session_markets(rows: list[dict], *, restart_ts: float, year: int) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        name = row["market"]
        if series_of(name) != "5m":
            continue
        if row["ts"] < restart_ts - 1:
            continue
        start = five_m_start_ts(name, year)
        if start is None:
            continue
        item = grouped.setdefault(
            name,
            {
                "market": name,
                "start_ts": start,
                "end_ts": start + WINDOW_S,
                "buys": [],
                "sells": [],
                "redeems": [],
            },
        )
        if row["action"] == "Buy":
            item["buys"].append(row)
        elif row["action"] == "Sell":
            item["sells"].append(row)
        elif row["action"] == "Redeem":
            item["redeems"].append(row)
    return sorted(grouped.values(), key=lambda m: m["start_ts"])


def session_pnl(market: dict) -> dict:
    spent = sum(b["usdc"] for b in market["buys"])
    sold = sum(s["usdc"] for s in market["sells"])
    redeemed = sum(r["usdc"] for r in market["redeems"])
    pnl = sold + redeemed - spent
    if market["sells"] and not market["redeems"]:
        outcome = "hedge"
    elif market["redeems"] and not market["sells"]:
        outcome = "redeem"
    elif market["sells"] and market["redeems"]:
        outcome = "mixed"
    else:
        outcome = "open"
    first = market["buys"][0] if market["buys"] else None
    return {
        "spent": spent,
        "sold": sold,
        "redeemed": redeemed,
        "pnl": pnl,
        "outcome": outcome,
        "fill_px": first["px"] if first else None,
        "leg": first["leg"] if first else None,
        "fill_ts": first["ts"] if first else None,
        "shares": sum(b["tok"] for b in market["buys"]),
    }


def five_m_windows(end_after: int, end_before: int) -> list[tuple[int, int, str]]:
    """Closed 5m windows with end_ts in (end_after, end_before]."""
    first_end = ((end_after // WINDOW_S) + 1) * WINDOW_S
    out = []
    end = first_end
    while end <= end_before:
        start = end - WINDOW_S
        out.append((start, end, f"{SLUG_PREFIX}-{end}"))
        end += WINDOW_S
    return out


def sample_from_window(
    btc: BtcSeries,
    *,
    slug: str,
    start_ts: int,
    end_ts: int,
    sample_ts: float,
    source: str,
    fill_px: Optional[float] = None,
    leg: Optional[str] = None,
    winner_hint: Optional[str] = None,
    outcome: str = "paper",
) -> Optional[Sample]:
    ptb = btc.at_or_before(start_ts)
    close_px = btc.at_or_before(end_ts)
    if ptb is None or close_px is None:
        return None
    feat = features_at(
        btc,
        ts=sample_ts,
        end_ts=end_ts,
        ptb=ptb,
        fill_px=fill_px,
        leg=leg,
    )
    if feat is None:
        return None
    winner = winner_hint or side_of(close_px, ptb)
    if winner is None:
        return None
    bought = (leg.lower() if leg else feat.side)
    reversed_ = winner != feat.side
    won = winner == bought
    px = fill_px if fill_px and fill_px > 0 else DEFAULT_FILL_PX
    return Sample(
        slug=slug,
        start_ts=start_ts,
        end_ts=end_ts,
        feat=feat,
        close_px=close_px,
        winner=winner,
        reversed=reversed_,
        soft_close=abs(close_px - ptb) < 10.0,
        source=source,
        pnl_redeem=paper_redeem_pnl(px, won),
        outcome=outcome,
    )


@dataclass
class BucketStats:
    n: int = 0
    flips: int = 0
    pnl: float = 0.0
    abs_dist: list[float] = field(default_factory=list)
    vol: list[float] = field(default_factory=list)

    def add(self, sample: Sample) -> None:
        self.n += 1
        self.flips += int(sample.reversed)
        self.pnl += sample.pnl_redeem
        self.abs_dist.append(sample.feat.abs_dist)
        if sample.feat.vol_30s is not None:
            self.vol.append(sample.feat.vol_30s)

    @property
    def flip_rate(self) -> Optional[float]:
        return (self.flips / self.n) if self.n else None

    @property
    def wr(self) -> Optional[float]:
        if not self.n:
            return None
        return 1.0 - (self.flips / self.n)


def tabulate(samples: Sequence[Sample], key_fn, order: Optional[Sequence[str]] = None) -> list[str]:
    groups: dict[str, BucketStats] = {}
    for sample in samples:
        key = key_fn(sample)
        groups.setdefault(key, BucketStats()).add(sample)
    keys = list(order) if order else sorted(groups, key=lambda k: (-groups[k].n, k))
    for key in groups:
        if key not in keys:
            keys.append(key)
    lines = ["bucket\tn\tflips\tflip_rate\twr\tpaper_pnl\tmean_|dist|\tmean_vol30"]
    for key in keys:
        st = groups.get(key)
        if st is None:
            continue
        lines.append(
            f"{key}\t{st.n}\t{st.flips}\t{pct(st.flip_rate)}\t{pct(st.wr)}\t"
            f"{money(st.pnl)}\t{(mean(st.abs_dist) or 0):.1f}\t"
            f"{(mean(st.vol) or 0):.2f}"
        )
    return lines


def skip_cost_table(samples: Sequence[Sample], predicate, label: str) -> str:
    kept = [s for s in samples if not predicate(s)]
    skipped = [s for s in samples if predicate(s)]
    base_pnl = sum(s.pnl_redeem for s in samples)
    kept_pnl = sum(s.pnl_redeem for s in kept)
    skip_pnl = sum(s.pnl_redeem for s in skipped)
    base_flips = sum(s.reversed for s in samples)
    kept_flips = sum(s.reversed for s in kept)
    n = len(samples)
    k = len(kept)
    sk = len(skipped)
    return (
        f"{label}\tkeep {k}/{n} ({(k / n if n else 0):.0%})\t"
        f"skip {sk} pnl {money(skip_pnl)}\t"
        f"base {money(base_pnl)} wr {pct(1 - base_flips / n if n else None)}\t"
        f"kept {money(kept_pnl)} wr {pct(1 - kept_flips / k if k else None)}\t"
        f"delta {money(kept_pnl - base_pnl)}"
    )


def fine_dist_order() -> list[str]:
    edges = FINE_DIST_EDGES
    return [
        bucket((a + b) / 2 if math.isfinite(b) else a + 1.0, edges)
        for a, b in zip(edges, edges[1:])
    ]


def gate_table(
    samples: Sequence[Sample],
    mins: Sequence[float] = GATE_MINS,
    *,
    fill_px: float = DEFAULT_FILL_PX,
    salvage: float = DEFAULT_SALVAGE,
    hours: Optional[float] = None,
) -> list[str]:
    """Keep if |dist| ≥ min. Flip rate of the *kept* set vs eatability at fill_px."""
    n = len(samples)
    cap0 = breakeven_flip_rate(fill_px, salvage=0.0)
    cap1 = breakeven_flip_rate(fill_px, salvage=salvage)
    header = (
        f"GATE keep |dist|≥X   fill={fill_px:.2f}  "
        f"eat_flips no-hedge≤{pct(cap0)}  "
        f"with ${salvage:.2f} salvage≤{pct(cap1)}"
    )
    lines = [
        header,
        "min_|dist|\tkeep\tskip\tkeep_pct\tskip_pct\tflips_kept\tflip_kept\t"
        "wr_kept\tpaper_pnl_kept\tev_nohedge\tev_salvage\teat_nohedge\teat_salvage"
        + ("\tkeeps_per_hour" if hours else ""),
    ]
    for floor in mins:
        kept = [s for s in samples if s.feat.abs_dist + 1e-12 >= float(floor)]
        k = len(kept)
        sk = n - k
        flips = sum(s.reversed for s in kept)
        fr = (flips / k) if k else None
        wr = (1.0 - fr) if fr is not None else None
        pnl = sum(s.pnl_redeem for s in kept)
        ev0 = paper_ev(fill_px, fr, salvage=0.0) if fr is not None else None
        ev1 = paper_ev(fill_px, fr, salvage=salvage) if fr is not None else None
        row = (
            f"{floor:g}\t{k}\t{sk}\t{pct(k / n if n else None)}\t{pct(sk / n if n else None)}\t"
            f"{flips}\t{pct(fr)}\t{pct(wr)}\t{money(pnl)}\t"
            f"{money(ev0)}\t{money(ev1)}\t"
            f"{eatable(fr, fill_px, 0.0)}\t{eatable(fr, fill_px, salvage)}"
        )
        if hours and hours > 0:
            row += f"\t{(k / hours):.1f}"
        lines.append(row)
    return lines


def breakeven_ref_table(
    fills: Sequence[float] = (0.75, 0.80, 0.85, 0.88, 0.90, 0.95),
    salvages: Sequence[float] = (0.0, 1.00),
    budget: float = BUDGET,
) -> list[str]:
    lines = [
        "BREAK-EVEN flip rate (EV=0 at this fill). Winner pays (1/p − 1)*budget.",
        "fill\twin_$\tno_hedge_max_flip\tsalvage=$1_max_flip\t"
        "25pct_flip_EV_nohedge\t25pct_flip_EV_salv$1",
    ]
    for p in fills:
        win = budget / p - budget
        cap0 = breakeven_flip_rate(p, 0.0, budget)
        cap1 = breakeven_flip_rate(p, 1.00, budget)
        ev25_0 = paper_ev(p, 0.25, 0.0, budget)
        ev25_1 = paper_ev(p, 0.25, 1.00, budget)
        lines.append(
            f"{p:.2f}\t{win:+.2f}\t{pct(cap0)}\t{pct(cap1)}\t"
            f"{money(ev25_0)}\t{money(ev25_1)}"
        )
    return lines


def late_band_touch(trades: Sequence[dict], start_ts: int, end_ts: int) -> Optional[dict]:
    """First last-print in the live late 75–90 band during the last 120s."""
    late0 = end_ts - LATE_TTM_S
    best = None
    for row in trades:
        ts = float(row["ts"])
        if ts < max(start_ts, late0) or ts > end_ts:
            continue
        px = float(row["px"])
        if px + 1e-12 < LATE_MIN or px - 1e-12 > LATE_MAX:
            continue
        if best is None or ts < float(best["ts"]):
            best = row
    return best


def render_session(markets: list[dict], samples: list[Sample]) -> str:
    lines = [
        "SESSION TAPE (5m only, post-restart)",
        "market\toutcome\tleg\tfill\tttm_s\tspent\texit\tpnl\t|dist|\tmom30\tvol30\tagainst\tcross?",
    ]
    by_name = {s.slug: s for s in samples}
    tot = 0.0
    n_h = n_r = n_o = 0
    for m in markets:
        rec = session_pnl(m)
        tot += rec["pnl"]
        if rec["outcome"] == "hedge":
            n_h += 1
        elif rec["outcome"] == "redeem":
            n_r += 1
        else:
            n_o += 1
        sample = by_name.get(m["market"])
        feat = sample.feat if sample else None
        ttm = (m["end_ts"] - rec["fill_ts"]) if rec["fill_ts"] else ""
        fill = f"{rec['fill_px']:.3f}" if rec["fill_px"] else ""
        lines.append(
            f"{m['market'].replace('Bitcoin Up or Down - ', '')}\t"
            f"{rec['outcome']}\t{rec['leg'] or ''}\t{fill}\t{ttm:.0f}\t"
            f"{rec['spent']:.2f}\t{(rec['sold'] + rec['redeemed']):.2f}\t"
            f"{money(rec['pnl'])}\t"
            f"{(feat.abs_dist if feat else 0):.1f}\t"
            f"{(feat.mom_30s if feat and feat.mom_30s is not None else 0):.1f}\t"
            f"{(feat.vol_30s if feat and feat.vol_30s is not None else 0):.2f}\t"
            f"{feat.against_30s if feat else ''}\t"
            f"{feat.cross_before_end if feat else ''}"
        )
    lines.append("")
    lines.append(
        f"n={len(markets)} redeem={n_r} hedge={n_h} other={n_o} "
        f"session_pnl={money(tot)} mean={money(tot / len(markets) if markets else None)}"
    )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", type=Path, help="Polymarket UI history CSV")
    p.add_argument("--restart-utc", default="2026-08-27T08:57:16+00:00")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--hours", type=float, default=48.0, help="Historical lookback ending now (UTC)")
    p.add_argument("--end-utc", default="", help="Override historical end (default: now)")
    p.add_argument("--binance-interval", default="1s", choices=("1s", "1m"))
    p.add_argument("--with-clob", action="store_true", help="Join public last-trades (late 75–90)")
    p.add_argument("--cache", type=Path, default=Path("/tmp/poly_reversal_cache"))
    p.add_argument("--out", type=Path, help="Write full report to this path")
    p.add_argument("--sample-ttm", type=float, default=90.0, help="BTC-only sample seconds before close")
    args = p.parse_args(list(argv) if argv is not None else None)

    http = http_session()
    report: list[str] = []

    now = parse_utc(args.end_utc) if args.end_utc else datetime.now(timezone.utc)
    hist_end = int(now.timestamp())
    hist_start = hist_end - int(args.hours * 3600)
    # Pad for PTB / momentum lookback.
    fetch_from = hist_start - WINDOW_S - 90
    btc = fetch_binance_klines(
        http,
        start_s=fetch_from,
        end_s=hist_end + 5,
        interval=args.binance_interval,
        cache_dir=args.cache / "binance",
    )
    report.append(
        f"binance {args.binance_interval} bars={len(btc.ts)} "
        f"[{fetch_from}..{hist_end}]"
    )
    if len(btc.ts) < 10:
        report.append("ERROR: not enough Binance bars")
        text = "\n".join(report) + "\n"
        print(text, end="")
        return 2

    session_samples: list[Sample] = []
    if args.csv:
        restart = parse_utc(args.restart_utc)
        rows = load_csv(args.csv)
        markets = session_markets(rows, restart_ts=restart.timestamp(), year=args.year)
        for m in markets:
            rec = session_pnl(m)
            if not rec["fill_ts"]:
                continue
            sample = sample_from_window(
                btc,
                slug=m["market"],
                start_ts=m["start_ts"],
                end_ts=m["end_ts"],
                sample_ts=rec["fill_ts"],
                source="session",
                fill_px=rec["fill_px"],
                leg=rec["leg"],
                outcome=rec["outcome"],
            )
            if sample is None:
                continue
            sample.pnl_redeem = rec["pnl"]
            session_samples.append(sample)
        report.append("")
        report.append(render_session(markets, session_samples))
        if session_samples:
            report.append("")
            report.append("SESSION vs BTC features at fill")
            hedges = [s for s in session_samples if s.outcome == "hedge"]
            redeems = [s for s in session_samples if s.outcome == "redeem"]
            def _avg(xs, attr):
                vals = [getattr(s.feat, attr) for s in xs if getattr(s.feat, attr) is not None]
                return mean(vals)
            report.append(
                f"redeem n={len(redeems)} mean_|dist|={(_avg(redeems, 'abs_dist') or 0):.1f} "
                f"mean_mom30={(_avg(redeems, 'mom_30s') or 0):.1f} "
                f"mean_vol30={(_avg(redeems, 'vol_30s') or 0):.2f} "
                f"against_share={sum(1 for s in redeems if s.feat.against_30s)/len(redeems) if redeems else 0:.0%}"
            )
            report.append(
                f"hedge  n={len(hedges)} mean_|dist|={(_avg(hedges, 'abs_dist') or 0):.1f} "
                f"mean_mom30={(_avg(hedges, 'mom_30s') or 0):.1f} "
                f"mean_vol30={(_avg(hedges, 'vol_30s') or 0):.2f} "
                f"against_share={sum(1 for s in hedges if s.feat.against_30s)/len(hedges) if hedges else 0:.0%}"
            )
            fills = [s.feat.fill_px for s in session_samples if s.feat.fill_px]
            mean_fill = mean(fills) or DEFAULT_FILL_PX
            t0 = min(m["start_ts"] for m in markets)
            t1 = max(m["end_ts"] for m in markets)
            hours = max((t1 - t0) / 3600.0, 1e-9)
            report.append("")
            report.append(
                f"SESSION mean fill {mean_fill:.3f}  span {hours:.2f}h  "
                f"fills/hour {len(session_samples) / hours:.1f}"
            )
            report.extend(breakeven_ref_table())
            report.append("")
            report.append("SESSION $5 |dist| buckets at fill")
            report.extend(
                tabulate(
                    session_samples,
                    lambda s: bucket(s.feat.abs_dist, FINE_DIST_EDGES),
                    order=fine_dist_order(),
                )
            )
            report.append("")
            report.append(
                "SESSION GATE — keep fills with |dist|≥X. "
                "paper_pnl_kept is actual wallet P&L (hedge salvage included). "
                "keeps_per_hour is this tape's fill rate after the gate."
            )
            report.extend(
                gate_table(
                    session_samples,
                    fill_px=mean_fill,
                    salvage=DEFAULT_SALVAGE,
                    hours=hours,
                )
            )
            for floor in (20, 25, 30, 40):
                skipped = [
                    s for s in session_samples if s.feat.abs_dist < float(floor)
                ]
                if not skipped:
                    continue
                report.append(f"session skipped |dist|<{floor:g}:")
                for s in skipped:
                    report.append(
                        f"  {s.outcome:6} |dist|={s.feat.abs_dist:5.1f} "
                        f"fill={s.feat.fill_px:.3f} ttm={s.feat.ttm:.0f} "
                        f"pnl={money(s.pnl_redeem)}"
                    )
            report.append("")
            report.extend(session_replay_table(session_samples, hours))

    windows = five_m_windows(hist_start, hist_end - 1)
    btc_samples: list[Sample] = []
    packed: list[tuple[float, float, list[tuple[int, float, float, float]]]] = []
    sample_ttm = float(args.sample_ttm)
    path_step = 1 if args.binance_interval == "1s" else 5
    for start, end, slug in windows:
        pack = window_path(btc, start, end, step=path_step)
        if pack is not None:
            packed.append(pack)
        sample_ts = end - sample_ttm
        if sample_ts <= start:
            continue
        sample = sample_from_window(
            btc,
            slug=slug,
            start_ts=start,
            end_ts=end,
            sample_ts=sample_ts,
            source="btc_ttm",
        )
        if sample:
            btc_samples.append(sample)
    report.append("")
    report.append(
        f"HISTORICAL BTC-ONLY sampled at T-{sample_ttm:.0f}s  "
        f"windows={len(windows)} scored={len(btc_samples)} "
        f"interval={args.binance_interval}"
    )
    report.append(
        "This is every 5m window, not only 75–90 books. Flip = BTC side of PTB "
        "at sample disagrees with close."
    )
    report.extend(tabulate(btc_samples, lambda s: "all", order=("all",)))
    report.append("")
    report.append("by |BTC−PTB| at sample (USD)")
    report.extend(
        tabulate(
            btc_samples,
            lambda s: bucket(s.feat.abs_dist, DIST_EDGES),
            order=[
                bucket((a + b) / 2 if math.isfinite(b) else a + 1, DIST_EDGES)
                for a, b in zip(DIST_EDGES, DIST_EDGES[1:])
            ],
        )
    )
    report.append("")
    report.append("by |BTC−PTB| $5 buckets at sample (USD)")
    report.extend(
        tabulate(
            btc_samples,
            lambda s: bucket(s.feat.abs_dist, FINE_DIST_EDGES),
            order=fine_dist_order(),
        )
    )
    hist_hours = (len(windows) / 12.0) if windows else None
    report.append("")
    report.append(
        "HISTORICAL GATE — keep windows with |dist|≥X at T-"
        f"{sample_ttm:.0f}s. keep% of ALL 5m windows (including 50/50 the bot "
        "would not buy) — treat skip% as an upper bound on fill loss. "
        "keeps_per_hour = passing windows / hour (max 12)."
    )
    report.extend(
        gate_table(
            btc_samples,
            fill_px=DEFAULT_FILL_PX,
            salvage=DEFAULT_SALVAGE,
            hours=hist_hours,
        )
    )
    report.append("")
    report.append("by 30s realized 1s-move std (USD)")
    report.extend(
        tabulate(
            btc_samples,
            lambda s: bucket(s.feat.vol_30s, VOL_EDGES),
        )
    )
    report.append("")
    report.append("by against-momentum (30s BTC move opposite the current side)")
    report.extend(
        tabulate(
            btc_samples,
            lambda s: "against" if s.feat.against_30s else ("with" if s.feat.against_30s is False else "na"),
            order=("against", "with", "na"),
        )
    )
    report.append("")
    report.append("by linear cross-before-close (vel from 30s mom toward PTB)")
    report.extend(
        tabulate(
            btc_samples,
            lambda s: "cross_before_end" if s.feat.cross_before_end else (
                "not_approaching" if s.feat.cross_before_end is False else "na"
            ),
            order=("cross_before_end", "not_approaching", "na"),
        )
    )
    report.append("")
    report.append("SKIP COST (paper $2.50 @ 85¢, redeem $1/$0, no hedge)")
    report.append(skip_cost_table(btc_samples, lambda s: s.feat.abs_dist < 10, "skip |dist|<$10"))
    report.append(skip_cost_table(btc_samples, lambda s: s.feat.abs_dist < 20, "skip |dist|<$20"))
    report.append(skip_cost_table(btc_samples, lambda s: s.feat.abs_dist < 40, "skip |dist|<$40"))
    report.append(skip_cost_table(btc_samples, lambda s: bool(s.feat.against_30s), "skip against-mom 30s"))
    report.append(skip_cost_table(btc_samples, lambda s: bool(s.feat.cross_before_end), "skip projected cross"))
    report.append("")
    report.append("by flip_z = |dist| / (vol_1s * sqrt(TTM))  (sigmas to PTB)")
    report.extend(
        tabulate(
            btc_samples,
            lambda s: bucket(s.feat.flip_z, Z_EDGES),
        )
    )
    report.append(skip_cost_table(btc_samples, lambda s: (s.feat.flip_z or 99) < 1, "skip z<1"))
    report.append(skip_cost_table(btc_samples, lambda s: (s.feat.flip_z or 99) < 2, "skip z<2"))
    report.append(
        skip_cost_table(
            btc_samples,
            lambda s: bool(s.feat.against_30s) and s.feat.abs_dist < 20,
            "skip against AND |dist|<$20",
        )
    )
    report.append(
        skip_cost_table(
            btc_samples,
            lambda s: (s.feat.vol_30s or 0) >= 8,
            "skip vol30>=$8",
        )
    )
    report.append(
        skip_cost_table(
            btc_samples,
            lambda s: s.feat.abs_dist < 10 or bool(s.feat.cross_before_end),
            "skip |dist|<$10 OR projected cross",
        )
    )
    mid = [s for s in btc_samples if 10.0 <= s.feat.abs_dist < 40.0]
    report.append("")
    report.append(
        f"MID |dist| $10–40 at T-{sample_ttm:.0f}s (closer analog to a 75–90 favorite) n={len(mid)}"
    )
    if mid:
        report.extend(
            tabulate(
                mid,
                lambda s: "against" if s.feat.against_30s else ("with" if s.feat.against_30s is False else "na"),
                order=("against", "with", "na"),
            )
        )
        report.extend(
            tabulate(
                mid,
                lambda s: "cross_before_end" if s.feat.cross_before_end else (
                    "not_approaching" if s.feat.cross_before_end is False else "na"
                ),
                order=("cross_before_end", "not_approaching", "na"),
            )
        )
        report.append(skip_cost_table(mid, lambda s: bool(s.feat.against_30s), "mid: skip against-mom"))
        report.append(skip_cost_table(mid, lambda s: bool(s.feat.cross_before_end), "mid: skip projected cross"))
        report.append(skip_cost_table(mid, lambda s: (s.feat.vol_30s or 0) >= 4, "mid: skip vol30>=$4"))
        report.append(skip_cost_table(mid, lambda s: (s.feat.flip_z or 99) < 2, "mid: skip z<2"))
        report.append("mid by flip_z")
        report.extend(tabulate(mid, lambda s: bucket(s.feat.flip_z, Z_EDGES)))

    hist_hours = (len(windows) / 12.0) if windows else 0.0
    if packed and hist_hours > 0:
        report.append("")
        report.append(
            "STRATEGY COMBOS — first tick in the TTM window with |BTC−PTB| ≥ edge. "
            "This is the live-shaped question (when + how decided), not a fixed T-90 cut. "
            "Fill price is implied from |dist| (session calibration). Losers return $1 salvage."
        )
        report.extend(score_combos(packed, hours=hist_hours, salvage=DEFAULT_SALVAGE))
        report.append("")
        report.extend(score_grid(packed, hours=hist_hours, salvage=DEFAULT_SALVAGE))
        report.append("")
        report.append("same combos, losers get $0 (no hedge):")
        report.extend(score_combos(packed, hours=hist_hours, salvage=0.0))
        report.append("")
        report.extend(score_grid(packed, hours=hist_hours, salvage=0.0))

    clob_samples: list[Sample] = []
    if args.with_clob:
        n_ok = n_miss = n_noband = n_side = 0
        cache_trades = args.cache / "trades"

        def _one(window: tuple[int, int, str]) -> tuple[str, Optional[Sample]]:
            start, end, slug = window
            gh = hedge_session()
            try:
                ev = fetch_event(gh, slug)
            except Exception:
                return "miss", None
            if not ev or not ev.get("condition"):
                return "miss", None
            try:
                trades = fetch_trades(gh, ev["condition"], start, end, cache_trades)
            except Exception:
                return "miss", None
            touch = late_band_touch(trades, start, end)
            if not touch:
                return "noband", None
            sample = sample_from_window(
                btc,
                slug=slug,
                start_ts=start,
                end_ts=end,
                sample_ts=float(touch["ts"]),
                source="clob_late",
                fill_px=float(touch["px"]),
                leg=str(touch.get("outcome") or ""),
                winner_hint=ev.get("winner"),
            )
            if sample is None:
                return "noband", None
            if sample.feat.leg and sample.feat.leg.lower() != sample.feat.side:
                return "side", None
            return "ok", sample

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(_one, w) for w in windows]
            for fut in as_completed(futs):
                kind, sample = fut.result()
                if kind == "ok" and sample is not None:
                    n_ok += 1
                    clob_samples.append(sample)
                elif kind == "miss":
                    n_miss += 1
                elif kind == "side":
                    n_side += 1
                else:
                    n_noband += 1
        report.append("")
        report.append(
            f"CLOB LATE-BAND 75–90 last {LATE_TTM_S:.0f}s  "
            f"hits={len(clob_samples)} gamma_ok={n_ok} no_band={n_noband} "
            f"side_mismatch={n_side} miss={n_miss}"
        )
        if windows and len(clob_samples) < 0.05 * len(windows):
            report.append(
                "WARN: public Data API /prices-history last-trades around 50¢ "
                "and miss known 75–90 wallet fills. Do not use --with-clob as the "
                "75–90 universe. Pathlog ticks on the VM are the book tape; "
                "BTC-only + the session CSV are the numbers to trust here."
            )
        if clob_samples:
            report.append("by |BTC−PTB| at first late-band print")
            report.extend(
                tabulate(
                    clob_samples,
                    lambda s: bucket(s.feat.abs_dist, DIST_EDGES),
                )
            )
            report.append("")
            report.append("by against-momentum at first late-band print")
            report.extend(
                tabulate(
                    clob_samples,
                    lambda s: "against" if s.feat.against_30s else ("with" if s.feat.against_30s is False else "na"),
                    order=("against", "with", "na"),
                )
            )
            report.append("")
            report.append("CLOB SKIP COST (fill at last-print, $2.50, redeem $1/$0)")
            report.append(skip_cost_table(clob_samples, lambda s: s.feat.abs_dist < 10, "skip |dist|<$10"))
            report.append(skip_cost_table(clob_samples, lambda s: s.feat.abs_dist < 20, "skip |dist|<$20"))
            report.append(skip_cost_table(clob_samples, lambda s: bool(s.feat.against_30s), "skip against-mom 30s"))
            report.append(skip_cost_table(clob_samples, lambda s: bool(s.feat.cross_before_end), "skip projected cross"))
            report.append(
                skip_cost_table(
                    clob_samples,
                    lambda s: bool(s.feat.against_30s) and s.feat.abs_dist < 20,
                    "skip against AND |dist|<$20",
                )
            )

    report.append("")
    report.append("NOTES")
    report.append("- Live 5m already requires TWAP vs PTB side match ($0 edge). This study asks whether |dist|, vol, or against-momentum add flip prediction on top of that.")
    report.append("- Binance 1s/1m is not Chainlink TWAP 30s. Directional level/vol should still rank; exact dollar thresholds will not match the live feed tick-for-tick.")
    report.append("- BTC-only rows include 50/50 windows the bot would never buy. CLOB late-band rows are the closer analog to a 75–90 fill.")
    report.append("- Combo $/h uses implied fill from |dist| (session-calibrated). live_shaped_union is early ≥90 analog + late 75–90 + last-45 ≥90 on that map.")
    report.append("- A live dump at 32¢ / persist-50 changes skip-cost in losers' favor (skipping a loser is worth less than $2.50).")
    report.append("- Do not copy these thresholds into live JSON from n<30 session tape.")
    report.append("")
    report.append("RECOMMENDATION (probe knobs; not live until the operator patches JSON)")
    report.append("  entry time: last 45s only (buy_start_s=45, late_90_start_s=45)")
    report.append("  disable early: early_buy_start_s=45, early_95_start_s=0, early_95_min_s=0")
    report.append("  ask: 75–90 plus last-45 ≥90 overlay (75–99 in the last 45s)")
    report.append("  oracle: min_underlying_edge_usd=25 (|TWAP−PTB|; this study used Binance)")
    report.append("  size: stay $2.50; $5 is ~2× $/h if fills hold (buy_budget=late_buy_budget=5, buy_max_spend=6)")
    report.append("  hedge: unchanged persist 5s @ 50/52, dump 32, recovery 53")
    report.append("  do not add a vol or against-momentum skip")

    text = "\n".join(report) + "\n"
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
