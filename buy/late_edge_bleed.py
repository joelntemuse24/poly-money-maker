"""Hourly |live−PTB| late-window bleed — analysis only, no trading.

Compares peak / distribution of |live − PTB| in TTM > 2m versus the last
≤2m, and whether the largest adverse move toward flat/flip (favor-edge
drawdown from the running peak) occurs in that late window.

Tape is reconstructed from Binance last-print vs window-open PTB. Hourly
pathlog is CLOB books for the last ~20m only and does not store live−PTB.
Research JSONL (`ptb_capture`) is the preferred PTB source when present;
it is not a dense hour tape (often a handful of skip/fill rows).
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCHEMA_VERSION = 1
EVENT_HOUR = "late_edge_bleed_hour"
EVENT_SUMMARY = "late_edge_bleed_summary"
LATE_TTM_S = 120.0
HOUR_S = 3600.0
MIN_EARLY_SAMPLES = 60
MIN_LATE_SAMPLES = 10

MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
MONTH_I = {name: i + 1 for i, name in enumerate(MONTHS)}
SLUG_RE = re.compile(
    r"^bitcoin-up-or-down-("
    + "|".join(MONTHS)
    + r")-(\d{1,2})-(\d{4})-(\d{1,2})(am|pm)-et$",
    re.I,
)


@dataclass(frozen=True)
class EdgeSample:
    ts: float
    ttm_s: float
    live_btc: float
    edge_usd: float  # live − ptb; + favors Up


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(sum(values) / len(values))


def parse_hourly_slug(slug: str) -> Optional[Tuple[float, float]]:
    """Return (start_ts, end_ts) for `bitcoin-up-or-down-{mon}-{d}-{y}-{h}{am|pm}-et`."""
    match = SLUG_RE.match((slug or "").strip())
    if not match:
        return None
    month = match.group(1).lower()
    day = int(match.group(2))
    year = int(match.group(3))
    hour12 = int(match.group(4))
    ap = match.group(5).lower()
    if month not in MONTH_I or hour12 < 1 or hour12 > 12:
        return None
    hour = hour12 % 12
    if ap == "pm":
        hour += 12
    try:
        start_dt = datetime(year, MONTH_I[month], day, hour, 0, 0, tzinfo=ET)
    except ValueError:
        return None
    end_dt = start_dt + timedelta(hours=1)
    return start_dt.timestamp(), end_dt.timestamp()


def format_hourly_slug(start_ts: float) -> str:
    dt = datetime.fromtimestamp(float(start_ts), tz=ET)
    hour = int(dt.hour)
    if hour == 0:
        label_h, ap = 12, "am"
    elif hour == 12:
        label_h, ap = 12, "pm"
    elif hour < 12:
        label_h, ap = hour, "am"
    else:
        label_h, ap = hour - 12, "pm"
    return (
        f"bitcoin-up-or-down-{MONTHS[dt.month - 1]}-{dt.day}-{dt.year}-"
        f"{label_h}{ap}-et"
    )


def iter_completed_hourly_windows(
    *,
    now: datetime,
    last_n: Optional[int] = None,
    hours: Optional[float] = None,
    today: bool = False,
) -> List[Tuple[str, float, float]]:
    """Completed ET hourly BTC windows. The in-progress hour is excluded."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(ET)
    cur_hour = et.replace(minute=0, second=0, microsecond=0)

    if last_n is not None and not today and hours is None:
        out: List[Tuple[str, float, float]] = []
        cursor = cur_hour
        for _ in range(max(0, int(last_n))):
            cursor = cursor - timedelta(hours=1)
            start = cursor.timestamp()
            end = (cursor + timedelta(hours=1)).timestamp()
            out.append((format_hourly_slug(start), start, end))
        out.reverse()
        return out

    if today:
        start_bound = et.replace(hour=0, minute=0, second=0, microsecond=0)
    elif hours is not None:
        start_bound = datetime.fromtimestamp(
            et.timestamp() - float(hours) * 3600.0, tz=ET
        ).replace(minute=0, second=0, microsecond=0)
    else:
        raise ValueError("need last_n, hours, or today")

    out = []
    cursor = start_bound
    cutoff = et.timestamp() - (float(hours) * 3600.0 if hours is not None else 0.0)
    while cursor < cur_hour:
        start = cursor.timestamp()
        end = (cursor + timedelta(hours=1)).timestamp()
        cursor = cursor + timedelta(hours=1)
        if hours is not None and end <= cutoff:
            continue
        out.append((format_hourly_slug(start), start, end))
    if last_n is not None:
        out = out[-max(0, int(last_n)) :]
    return out


def edge_samples(
    path: Sequence[Tuple[float, float]],
    *,
    start_ts: float,
    end_ts: float,
    ptb: float,
) -> List[EdgeSample]:
    """(ts, live) → signed edge samples. Drops the window-open print (ts ≤ start)."""
    ptb_f = float(ptb)
    out: List[EdgeSample] = []
    for ts, px in path:
        try:
            ts_f = float(ts)
            px_f = float(px)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(ts_f) or not math.isfinite(px_f):
            continue
        if ts_f <= float(start_ts) or ts_f > float(end_ts):
            continue
        ttm = float(end_ts) - ts_f
        if ttm < 0:
            continue
        out.append(
            EdgeSample(
                ts=ts_f,
                ttm_s=ttm,
                live_btc=px_f,
                edge_usd=px_f - ptb_f,
            )
        )
    out.sort(key=lambda sample: sample.ts)
    return out


def split_early_late(
    samples: Sequence[EdgeSample],
    late_ttm_s: float = LATE_TTM_S,
) -> Tuple[List[EdgeSample], List[EdgeSample]]:
    cutoff = float(late_ttm_s)
    early = [s for s in samples if s.ttm_s > cutoff]
    late = [s for s in samples if 0.0 <= s.ttm_s <= cutoff]
    return early, late


def window_stats(samples: Sequence[EdgeSample]) -> Dict[str, Any]:
    if not samples:
        return {
            "n": 0,
            "peak_abs": None,
            "min_abs": None,
            "median_abs": None,
            "mean_abs": None,
            "first_abs": None,
            "last_abs": None,
            "abs_change": None,
            "peak_ttm_s": None,
        }
    abs_vals = [abs(s.edge_usd) for s in samples]
    peak_i = max(range(len(samples)), key=lambda i: (abs_vals[i], -samples[i].ts))
    first_abs = abs_vals[0]
    last_abs = abs_vals[-1]
    return {
        "n": len(samples),
        "peak_abs": abs_vals[peak_i],
        "min_abs": min(abs_vals),
        "median_abs": _median(abs_vals),
        "mean_abs": _mean(abs_vals),
        "first_abs": first_abs,
        "last_abs": last_abs,
        "abs_change": last_abs - first_abs,
        "peak_ttm_s": samples[peak_i].ttm_s,
    }


def _side_of(edge: float) -> Optional[str]:
    if edge > 0:
        return "up"
    if edge < 0:
        return "down"
    return None


def _favor_drawdowns(
    samples: Sequence[EdgeSample],
    *,
    peak_sign: float,
    late_ttm_s: float,
) -> Tuple[float, float, float]:
    """Return (early_bleed, late_bleed, hour_max_dd) on peak-side favor-edge."""
    running_peak = None
    max_dd_early = 0.0
    max_dd_late = 0.0
    dd_entering_late: Optional[float] = None
    hour_max_dd = 0.0
    cutoff = float(late_ttm_s)
    for sample in samples:
        favor = peak_sign * sample.edge_usd
        if running_peak is None or favor > running_peak:
            running_peak = favor
        dd = running_peak - favor
        if dd > hour_max_dd:
            hour_max_dd = dd
        if sample.ttm_s > cutoff:
            if dd > max_dd_early:
                max_dd_early = dd
        else:
            if dd_entering_late is None:
                dd_entering_late = dd
            if dd > max_dd_late:
                max_dd_late = dd
    late_bleed = 0.0
    if dd_entering_late is not None:
        late_bleed = max(0.0, max_dd_late - dd_entering_late)
    return max_dd_early, late_bleed, hour_max_dd


def hour_report(
    samples: Sequence[EdgeSample],
    *,
    slug: str,
    start_ts: float,
    end_ts: float,
    ptb: float,
    ptb_source: str,
    tape: str,
    late_ttm_s: float = LATE_TTM_S,
    min_early: int = MIN_EARLY_SAMPLES,
    min_late: int = MIN_LATE_SAMPLES,
) -> Dict[str, Any]:
    ordered = sorted(samples, key=lambda s: s.ts)
    early, late = split_early_late(ordered, late_ttm_s)
    base = {
        "event": EVENT_HOUR,
        "schema": SCHEMA_VERSION,
        "slug": slug,
        "start_ts": float(start_ts),
        "end_ts": float(end_ts),
        "ptb": float(ptb),
        "ptb_source": ptb_source,
        "tape": tape,
        "late_ttm_s": float(late_ttm_s),
        "n_early": len(early),
        "n_late": len(late),
        "ok": False,
        "reason": None,
        "max_adverse_in_late": None,
        "early_bleed": None,
        "late_bleed": None,
        "early": window_stats(early),
        "late": window_stats(late),
    }
    if not ordered:
        base["reason"] = "no_samples"
        return base
    if len(early) < int(min_early) or len(late) < int(min_late):
        base["reason"] = "sparse_tape"
        return base

    peak = max(ordered, key=lambda s: (abs(s.edge_usd), -s.ts))
    peak_side = _side_of(peak.edge_usd)
    peak_sign = 1.0 if peak_side == "up" else (-1.0 if peak_side == "down" else 0.0)
    early_bleed, late_bleed, hour_dd = _favor_drawdowns(
        ordered, peak_sign=peak_sign, late_ttm_s=late_ttm_s
    )
    last = ordered[-1]
    base.update(
        {
            "ok": True,
            "peak_side": peak_side,
            "peak_abs_hour": abs(peak.edge_usd),
            "peak_abs_ttm_s": peak.ttm_s,
            "peak_favor_edge": peak_sign * peak.edge_usd,
            "close_edge": last.edge_usd,
            "close_abs": abs(last.edge_usd),
            "close_side": _side_of(last.edge_usd),
            "early_bleed": early_bleed,
            "late_bleed": late_bleed,
            "hour_favor_dd": hour_dd,
            "max_adverse_in_late": late_bleed > early_bleed + 1e-12,
        }
    )
    return base


def summarize_hours(hours: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    ok_rows = [row for row in hours if row.get("ok")]
    late_flags = [bool(row.get("max_adverse_in_late")) for row in ok_rows]
    late_chg = [
        _f((row.get("late") or {}).get("abs_change"))
        for row in ok_rows
    ]
    early_chg = [
        _f((row.get("early") or {}).get("abs_change"))
        for row in ok_rows
    ]
    late_bleed = [_f(row.get("late_bleed")) for row in ok_rows]
    early_bleed = [_f(row.get("early_bleed")) for row in ok_rows]
    n_ok = len(ok_rows)
    n_late = sum(1 for flag in late_flags if flag)
    return {
        "event": EVENT_SUMMARY,
        "schema": SCHEMA_VERSION,
        "n_hours": n_ok,
        "n_skipped": len(hours) - n_ok,
        "n_max_adverse_in_late": n_late,
        "pct_max_adverse_in_late": (n_late / n_ok) if n_ok else None,
        "median_late_abs_change": _median([v for v in late_chg if v is not None]),
        "median_early_abs_change": _median([v for v in early_chg if v is not None]),
        "median_late_bleed": _median([v for v in late_bleed if v is not None]),
        "median_early_bleed": _median([v for v in early_bleed if v is not None]),
        "late_ttm_s": LATE_TTM_S,
    }


def render_text(
    hours: Sequence[Dict[str, Any]],
    summary: Dict[str, Any],
) -> str:
    lines = [
        "late_edge_bleed  tape=binance_1s_vs_ptb  late_ttm_s="
        f"{summary.get('late_ttm_s', LATE_TTM_S):g}  "
        f"schema={SCHEMA_VERSION}",
        (
            f"n_hours={summary.get('n_hours')}  skipped={summary.get('n_skipped')}  "
            f"pct_max_adverse_in_late="
            f"{_pct(summary.get('pct_max_adverse_in_late'))}  "
            f"median_late_abs_change={_money(summary.get('median_late_abs_change'))}  "
            f"median_early_abs_change={_money(summary.get('median_early_abs_change'))}  "
            f"median_late_bleed={_money(summary.get('median_late_bleed'))}  "
            f"median_early_bleed={_money(summary.get('median_early_bleed'))}"
        ),
        "",
        "slug\tok\tlate_adv\tearly_peak\tlate_last\tearly_d|e|\tlate_d|e|\tearly_bleed\tlate_bleed\tpeak_side\tclose_side",
    ]
    for row in hours:
        late = row.get("late") or {}
        early = row.get("early") or {}
        flag = row.get("max_adverse_in_late")
        late_lab = "LATE" if flag else ("early" if flag is False else "-")
        lines.append(
            "\t".join(
                [
                    str(row.get("slug") or ""),
                    "1" if row.get("ok") else f"0:{row.get('reason') or '?'}",
                    late_lab,
                    _money(early.get("peak_abs")),
                    _money(late.get("last_abs")),
                    _money(early.get("abs_change")),
                    _money(late.get("abs_change")),
                    _money(row.get("early_bleed")),
                    _money(row.get("late_bleed")),
                    str(row.get("peak_side") or "-"),
                    str(row.get("close_side") or "-"),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _money(value: Any) -> str:
    num = _f(value)
    if num is None:
        return "-"
    return f"{num:+.2f}"


def _pct(value: Any) -> str:
    num = _f(value)
    if num is None:
        return "-"
    return f"{100.0 * num:.1f}%"


def load_research_ptb(path: Path) -> Dict[str, Tuple[float, str]]:
    """slug → (ptb, source) from `ptb_capture` rows. Last write wins."""
    out: Dict[str, Tuple[float, str]] = {}
    if not path.is_file():
        return out
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return out
    with handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "ptb_capture":
                continue
            slug = str(row.get("slug") or "")
            ptb = _f(row.get("ptb"))
            if not slug or ptb is None:
                continue
            src = str(row.get("source") or row.get("ptb_source") or "") or "research_ptb_capture"
            out[slug] = (ptb, src)
    return out


def upsert_hour_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    """Replace per-slug hour events; drop prior summary rows."""
    dest = Path(path)
    existing: List[Dict[str, Any]] = []
    if dest.is_file():
        try:
            text = dest.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                existing.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    by_slug: Dict[str, Dict[str, Any]] = {}
    extras: List[Dict[str, Any]] = []
    for row in existing:
        event = row.get("event")
        slug = str(row.get("slug") or "")
        if event == EVENT_SUMMARY:
            continue
        if event == EVENT_HOUR and slug:
            by_slug[slug] = row
        else:
            extras.append(row)
    for row in rows:
        event = row.get("event")
        slug = str(row.get("slug") or "")
        if event == EVENT_SUMMARY:
            extras.append(row)
            continue
        if event == EVENT_HOUR and slug:
            by_slug[slug] = row
        else:
            extras.append(row)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(dest) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for row in extras:
            handle.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        for slug in sorted(by_slug):
            handle.write(
                json.dumps(by_slug[slug], separators=(",", ":"), default=str) + "\n"
            )
    os.replace(tmp, dest)
