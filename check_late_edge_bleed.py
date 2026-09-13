#!/usr/bin/env python3
"""Going-forward hourly |live−PTB| late-window bleed (last 2m vs prior 58m).

No orders. No strategy / hedge / 15m changes. Reconstructs the hour from
Binance 1s last-print vs PTB (research `ptb_capture` when present, else
Binance at-or-before window open — same convention as check_reversal_features).

Hourly pathlog ticks are CLOB books for the last ~20m and are not an
|edge| tape. underlying_research_buyhourly.jsonl is used for PTB alignment
only.

On the VM:
  .venv/bin/python check_late_edge_bleed.py --last 12
  .venv/bin/python check_late_edge_bleed.py --today
  .venv/bin/python check_late_edge_bleed.py --hours 24
  .venv/bin/python check_late_edge_bleed.py --slug bitcoin-up-or-down-september-13-2026-8am-et
  .venv/bin/python check_late_edge_bleed.py --last 12 --write

`--write` upserts `late_edge_bleed.jsonl` (gitignored) so a post-hour
routine can read `late_edge_bleed_hour` / `late_edge_bleed_summary`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from buy.late_edge_bleed import (
    EVENT_SUMMARY,
    LATE_TTM_S,
    edge_samples,
    format_hourly_slug,
    hour_report,
    iter_completed_hourly_windows,
    load_research_ptb,
    parse_hourly_slug,
    render_text,
    summarize_hours,
    upsert_hour_jsonl,
)
from check_reversal_features import BtcSeries, fetch_binance_klines, http_session

REPO = Path(__file__).resolve().parent
RESEARCH_DEFAULT = REPO / "underlying_research_buyhourly.jsonl"
WRITE_DEFAULT = REPO / "late_edge_bleed.jsonl"
CACHE_DEFAULT = Path("/tmp/poly_late_edge_bleed_cache")


def _windows_from_args(args: argparse.Namespace, now: datetime) -> List[Tuple[str, float, float]]:
    if args.slug:
        parsed = parse_hourly_slug(args.slug)
        if parsed is None:
            raise SystemExit(f"unrecognized hourly slug: {args.slug}")
        start, end = parsed
        return [(args.slug, start, end)]
    today = bool(args.today)
    last_n = args.last
    hours = args.hours
    if last_n is None and hours is None and not today:
        last_n = 12
    return iter_completed_hourly_windows(
        now=now, last_n=last_n, hours=hours, today=today
    )


def _path_from_series(
    btc: BtcSeries, start_ts: float, end_ts: float
) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    if not btc.ts:
        return out
    # Inclusive (start, end]; 1s stamps from the kline closer.
    lo = 0
    hi = len(btc.ts)
    start = float(start_ts)
    end = float(end_ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if btc.ts[mid] <= start:
            lo = mid + 1
        else:
            hi = mid
    i = lo
    while i < len(btc.ts) and btc.ts[i] <= end:
        out.append((float(btc.ts[i]), float(btc.px[i])))
        i += 1
    return out


def score_windows(
    windows: Sequence[Tuple[str, float, float]],
    *,
    btc: BtcSeries,
    research_ptb: Dict[str, Tuple[float, str]],
    ptb_override: Optional[float],
    interval: str,
    late_ttm_s: float,
) -> List[Dict[str, Any]]:
    reports: List[Dict[str, Any]] = []
    tape = f"binance_{interval}"
    for slug, start_ts, end_ts in windows:
        if ptb_override is not None:
            ptb, src = float(ptb_override), "cli_ptb"
        elif slug in research_ptb:
            ptb, src = research_ptb[slug]
        else:
            px = btc.at_or_before(start_ts)
            if px is None:
                reports.append(
                    hour_report(
                        [],
                        slug=slug,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        ptb=0.0,
                        ptb_source="missing",
                        tape=tape,
                        late_ttm_s=late_ttm_s,
                    )
                )
                reports[-1]["reason"] = "missing_ptb"
                continue
            ptb, src = float(px), "binance_open"
        path = _path_from_series(btc, start_ts, end_ts)
        samples = edge_samples(path, start_ts=start_ts, end_ts=end_ts, ptb=ptb)
        reports.append(
            hour_report(
                samples,
                slug=slug,
                start_ts=start_ts,
                end_ts=end_ts,
                ptb=ptb,
                ptb_source=src,
                tape=tape,
                late_ttm_s=late_ttm_s,
            )
        )
    return reports


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--last", type=int, default=None, help="Last N completed ET hours")
    parser.add_argument("--hours", type=float, default=None, help="Lookback ending now (completed hours)")
    parser.add_argument("--today", action="store_true", help="Completed ET hours since local midnight")
    parser.add_argument("--slug", default="", help="One hourly market slug")
    parser.add_argument("--end-utc", default="", help="Override now (ISO UTC)")
    parser.add_argument("--ptb", type=float, default=None, help="Force PTB for the selected window(s)")
    parser.add_argument("--late-ttm-s", type=float, default=LATE_TTM_S)
    parser.add_argument("--interval", default="1s", choices=("1s", "1m"))
    parser.add_argument("--research", type=Path, default=RESEARCH_DEFAULT)
    parser.add_argument("--cache", type=Path, default=CACHE_DEFAULT)
    parser.add_argument(
        "--write",
        nargs="?",
        const=str(WRITE_DEFAULT),
        default=None,
        help=f"Upsert JSONL (default {WRITE_DEFAULT.name})",
    )
    parser.add_argument("--json", action="store_true", help="Print hours+summary as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.interval == "1m":
        print(
            "warning: 1m bars leave ~2 late samples; prefer --interval 1s",
            file=sys.stderr,
        )

    now = (
        datetime.fromisoformat(args.end_utc.replace("Z", "+00:00"))
        if args.end_utc
        else datetime.now(timezone.utc)
    )
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    try:
        windows = _windows_from_args(args, now)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not windows:
        print("no completed hourly windows in range", file=sys.stderr)
        return 2

    fetch_start = int(min(w[1] for w in windows)) - 5
    fetch_end = int(max(w[2] for w in windows)) + 2
    http = http_session()
    btc = fetch_binance_klines(
        http,
        start_s=fetch_start,
        end_s=fetch_end,
        interval=args.interval,
        cache_dir=Path(args.cache) / "binance",
    )
    research_ptb = load_research_ptb(Path(args.research))
    hours = score_windows(
        windows,
        btc=btc,
        research_ptb=research_ptb,
        ptb_override=args.ptb,
        interval=args.interval,
        late_ttm_s=float(args.late_ttm_s),
    )
    summary = summarize_hours(hours)
    summary["tape"] = f"binance_{args.interval}"
    summary["generated_ts"] = now.timestamp()
    if windows:
        summary["from_slug"] = windows[0][0]
        summary["to_slug"] = windows[-1][0]

    if args.write:
        dest = Path(args.write)
        upsert_hour_jsonl(dest, list(hours) + [summary])
        print(f"wrote {dest} hours={summary.get('n_hours')} skipped={summary.get('n_skipped')}", file=sys.stderr)

    if args.json:
        print(json.dumps({"hours": hours, "summary": summary}, default=str, indent=2))
    else:
        print(render_text(hours, summary), end="")
        if not any(h.get("ok") for h in hours) and btc.ts:
            print(
                f"# binance bars={len(btc.ts)} [{fetch_start}..{fetch_end}] "
                f"slug0={format_hourly_slug(windows[0][1])}",
                file=sys.stderr,
            )
        elif not btc.ts:
            print("no binance bars in range", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
