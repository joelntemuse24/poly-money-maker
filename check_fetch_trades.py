#!/usr/bin/env python3
"""Fetch full Polymarket Data API trade history for a wallet.

The Polymarket UI export caps around ~500 rows. ``GET /trades`` allows
``limit`` up to 10000, but ``offset`` is capped at 10000 *per*
``start``/``end`` window (HTTP 400 past that — never clamped). Deeper
history is fetched by walking time windows: set ``end`` to the oldest
timestamp seen, reset offset, repeat.

Output CSV uses the same column names as the UI history export so
``check_participation.py --csv`` can load buys without a converter:

    timestamp    unix seconds (float-parseable; required by load_csv_buys)
    action       Buy / Sell
    usdcAmount   size * price (or API usdcSize)
    tokenAmount  fill size
    marketName   title
    tokenName    outcome

    python check_fetch_trades.py --user 0xYOUR... --out exports/trades.csv
    python check_fetch_trades.py --out exports/trades.csv   # FUNDER_ADDRESS
    python check_participation.py --hours 72 --csv exports/trades.csv

Re-runs merge/dedupe into the output file (key: tx hash + asset + unix
ts + size + side). Wallet comes from ``--user`` or ``FUNDER_ADDRESS`` in
``.env`` — never committed. Data API only; no subgraph fallback.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

TRADES_URL = "https://data-api.polymarket.com/trades"
USER_AGENT = "Mozilla/5.0 (compatible; poly-money-maker-fetch-trades/1.0)"
OFFSET_CAP = 10_000
DEFAULT_LIMIT = 1000
# Positive epoch so user-scoped requests return full history, not the
# default ~3-year public window. See Data API /trades docs.
FULL_HISTORY_START = 1

# DictReader columns used by check_participation.load_csv_buys, plus the
# Data API / UI history fields operators already know.
CSV_COLUMNS = [
    "proxyWallet",
    "timestamp",
    "timestampUtc",
    "conditionId",
    "type",
    "size",
    "usdcSize",
    "transactionHash",
    "price",
    "asset",
    "side",
    "outcomeIndex",
    "title",
    "slug",
    "icon",
    "eventSlug",
    "outcome",
    "name",
    "pseudonym",
    "bio",
    "profileImage",
    "profileImageOptimized",
    "action",
    "hash",
    "usdValue",
    "tokenName",
    "tokenAmount",
    "usdcAmount",
    "orderHash",
    "maker",
    "migratedTimestamp",
    "marketName",
]

DEDUP_KEYS = ("transactionHash", "asset", "timestamp", "size", "side")


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.4,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _fmt_ts(raw: Any) -> str:
    """Unix seconds → ``YYYY-MM-DD HH:MM:SS`` UTC (human-readable only)."""
    ts = _unix_ts(raw)
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _unix_ts(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _float(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _fmt_usdc(value: float) -> str:
    return f"{value:.8f}"


def trade_dedup_key(row: dict) -> tuple:
    return tuple(_str(row.get(k)) for k in DEDUP_KEYS)


def normalize_trade(raw: dict) -> dict:
    """Map Data API /trades JSON to UI-history / participation CSV columns."""
    size = _float(raw.get("size")) or 0.0
    price = _float(raw.get("price")) or 0.0
    usdc = _float(raw.get("usdcSize"))
    if usdc is None:
        usdc = size * price
    side = _str(raw.get("side")).upper()
    action = "Buy" if side == "BUY" else "Sell" if side == "SELL" else side.title()
    title = _str(raw.get("title"))
    outcome = _str(raw.get("outcome"))
    tx = _str(raw.get("transactionHash"))
    ts_unix = _unix_ts(raw.get("timestamp"))
    ts_out = str(ts_unix) if ts_unix is not None else _str(raw.get("timestamp"))
    return {
        "proxyWallet": _str(raw.get("proxyWallet")),
        # unix seconds — load_csv_buys does float(timestamp)
        "timestamp": ts_out,
        "timestampUtc": _fmt_ts(ts_unix),
        "_unix": ts_unix if ts_unix is not None else 0,
        "conditionId": _str(raw.get("conditionId")),
        "type": _str(raw.get("type") or "TRADE"),
        "size": _str(raw.get("size")),
        "usdcSize": _fmt_usdc(usdc),
        "transactionHash": tx,
        "price": _str(raw.get("price")),
        "asset": _str(raw.get("asset")),
        "side": side,
        "outcomeIndex": _str(raw.get("outcomeIndex")),
        "title": title,
        "slug": _str(raw.get("slug")),
        "icon": _str(raw.get("icon")),
        "eventSlug": _str(raw.get("eventSlug")),
        "outcome": outcome,
        "name": _str(raw.get("name")),
        "pseudonym": _str(raw.get("pseudonym")),
        "bio": _str(raw.get("bio")),
        "profileImage": _str(raw.get("profileImage")),
        "profileImageOptimized": _str(raw.get("profileImageOptimized")),
        "action": action,
        "hash": tx,
        "usdValue": _fmt_usdc(usdc),
        "tokenName": outcome,
        "tokenAmount": _str(raw.get("size")),
        "usdcAmount": _fmt_usdc(usdc),
        "orderHash": "",
        "maker": "",
        "migratedTimestamp": "",
        "marketName": title,
    }


def next_window_end(oldest_unix: int, *, made_progress: bool) -> int:
    """Advance ``end`` for the next time window.

    The API's ``end`` is inclusive, so overlapping the oldest fill is
    required. If a window produced no new unique rows, step one second
    older so pagination cannot stall on a dense timestamp.
    """
    if oldest_unix <= 0:
        raise ValueError("oldest_unix must be positive")
    if made_progress:
        return oldest_unix
    return oldest_unix - 1


def csv_field(row: dict, key: str) -> str:
    if key.startswith("_"):
        return ""
    return _str(row.get(key, ""))


def load_existing_keys(path: Path) -> set[tuple]:
    keys: set[tuple] = set()
    if not path.is_file() or path.stat().st_size == 0:
        return keys
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            keys.add(trade_dedup_key(row))
    return keys


def write_csv_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        fh.flush()
        os.fsync(fh.fileno())


def append_csv_rows(path: Path, rows: Iterable[dict]) -> int:
    n = 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        for row in rows:
            writer.writerow({k: csv_field(row, k) for k in CSV_COLUMNS})
            n += 1
        fh.flush()
        os.fsync(fh.fileno())
    return n


class OffsetCapError(RuntimeError):
    """Data API rejected offset past the per-window cap."""


def _is_offset_cap_error(status: int, body: str) -> bool:
    if status not in (400, 422):
        return False
    text = (body or "").lower()
    return "offset" in text and ("exceeded" in text or "10000" in text)


def fetch_page(
    session: requests.Session,
    *,
    user: str,
    limit: int,
    offset: int,
    start: Optional[int],
    end: Optional[int],
    taker_only: Optional[bool],
    side: Optional[str],
    market: Optional[str],
    timeout: float,
) -> list[dict]:
    params: dict[str, Any] = {
        "user": user,
        "limit": limit,
        "offset": offset,
    }
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    if taker_only is not None:
        params["takerOnly"] = str(bool(taker_only)).lower()
    if side:
        params["side"] = side
    if market:
        params["market"] = market

    resp = session.get(TRADES_URL, params=params, timeout=timeout)
    body = resp.text if resp.text is not None else ""
    if _is_offset_cap_error(resp.status_code, body):
        raise OffsetCapError(
            f"HTTP {resp.status_code} at offset={offset} (per-window cap is {OFFSET_CAP})"
        )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected /trades payload type: {type(data).__name__}")
    return data


def fetch_all_trades(
    session: requests.Session,
    *,
    user: str,
    out_path: Path,
    limit: int = DEFAULT_LIMIT,
    start: int = FULL_HISTORY_START,
    end: Optional[int] = None,
    taker_only: Optional[bool] = None,
    side: Optional[str] = None,
    market: Optional[str] = None,
    sleep_s: float = 0.25,
    timeout: float = 30.0,
    max_windows: int = 500,
    offset_cap: int = OFFSET_CAP,
) -> dict[str, Any]:
    existing = load_existing_keys(out_path)
    preexisting = len(existing)
    if not out_path.is_file() or out_path.stat().st_size == 0:
        write_csv_header(out_path)

    fetched = 0
    unique_new = 0
    pages = 0
    windows = 0
    newest_unix: Optional[int] = None
    oldest_unix: Optional[int] = None
    window_end = end
    offset_cap_hits = 0

    print(
        f"fetch start user={user[:10]}… out={out_path} "
        f"existing={preexisting} limit={limit} start={start} end={window_end}",
        flush=True,
    )

    while windows < max_windows:
        windows += 1
        offset = 0
        window_oldest: Optional[int] = None
        window_rows = 0
        window_new = 0
        hit_offset_cap = False
        window_complete = False

        while offset <= offset_cap:
            try:
                raw_page = fetch_page(
                    session,
                    user=user,
                    limit=limit,
                    offset=offset,
                    start=start,
                    end=window_end,
                    taker_only=taker_only,
                    side=side,
                    market=market,
                    timeout=timeout,
                )
            except OffsetCapError as exc:
                print(f"  offset cap: {exc}", flush=True)
                hit_offset_cap = True
                offset_cap_hits += 1
                break

            pages += 1
            if not raw_page:
                print(
                    f"  window {windows} offset={offset} empty page — window done",
                    flush=True,
                )
                window_complete = True
                break

            batch: list[dict] = []
            for raw in raw_page:
                fetched += 1
                row = normalize_trade(raw)
                ts = int(row.get("_unix") or 0)
                if ts:
                    if newest_unix is None or ts > newest_unix:
                        newest_unix = ts
                    if oldest_unix is None or ts < oldest_unix:
                        oldest_unix = ts
                    if window_oldest is None or ts < window_oldest:
                        window_oldest = ts
                key = trade_dedup_key(row)
                if key in existing:
                    continue
                existing.add(key)
                batch.append(row)

            written = append_csv_rows(out_path, batch) if batch else 0
            unique_new += written
            window_rows += len(raw_page)
            window_new += written
            print(
                f"  window {windows} offset={offset} got={len(raw_page)} "
                f"new={written} unique_total={preexisting + unique_new}",
                flush=True,
            )

            if len(raw_page) < limit:
                window_complete = True
                break
            offset += limit
            if offset > offset_cap:
                hit_offset_cap = True
                offset_cap_hits += 1
                print(
                    f"  offset would exceed {offset_cap}; closing window",
                    flush=True,
                )
                break
            if sleep_s > 0:
                time.sleep(sleep_s)

        if window_oldest is None:
            print("no more trades in this window — done", flush=True)
            break

        if window_complete and not hit_offset_cap:
            print(
                f"window {windows} complete ({window_rows} rows, {window_new} new) — history exhausted",
                flush=True,
            )
            break

        made_progress = window_new > 0
        try:
            nxt = next_window_end(window_oldest, made_progress=made_progress)
        except ValueError:
            break
        if window_end is not None and nxt >= window_end:
            nxt = window_end - 1
        if nxt < start:
            print(f"next end={nxt} is before start={start} — done", flush=True)
            break
        if nxt == window_end:
            print("window end did not advance — stop to avoid loop", flush=True)
            break
        window_end = nxt
        print(f"next window end={window_end} ({_fmt_ts(window_end)})", flush=True)
        if sleep_s > 0:
            time.sleep(sleep_s)

    return {
        "fetched": fetched,
        "unique_new": unique_new,
        "unique_total": preexisting + unique_new,
        "preexisting": preexisting,
        "pages": pages,
        "windows": windows,
        "offset_cap_hits": offset_cap_hits,
        "oldest_unix": oldest_unix,
        "newest_unix": newest_unix,
        "out": str(out_path),
    }


def summarize_csv(path: Path) -> dict[str, Any]:
    n = 0
    buys = 0
    sells = 0
    oldest_unix: Optional[int] = None
    newest_unix: Optional[int] = None
    series: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            n += 1
            action = (row.get("action") or "").strip()
            if action.lower() == "buy":
                buys += 1
            elif action.lower() == "sell":
                sells += 1
            ts = _unix_ts(row.get("timestamp"))
            if ts is not None:
                if oldest_unix is None or ts < oldest_unix:
                    oldest_unix = ts
                if newest_unix is None or ts > newest_unix:
                    newest_unix = ts
            slug = (row.get("slug") or "").lower()
            if "btc-updown-5m" in slug:
                key = "5m"
            elif "btc-updown" in slug:
                key = "15m/hourly"
            else:
                key = "other"
            series[key] = series.get(key, 0) + 1
    return {
        "rows": n,
        "buys": buys,
        "sells": sells,
        "oldest": _fmt_ts(oldest_unix) if oldest_unix else "",
        "newest": _fmt_ts(newest_unix) if newest_unix else "",
        "oldest_unix": oldest_unix,
        "newest_unix": newest_unix,
        "series": series,
    }


def default_out_path(user: str) -> Path:
    prefix = user.lower().replace("0x", "")[:8]
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Path("exports") / f"trades_{prefix}_{day}.csv"


def _parse_bool_arg(value: str) -> bool:
    val = str(value).strip().lower()
    if val in ("1", "true", "yes", "y"):
        return True
    if val in ("0", "false", "no", "n"):
        return False
    raise argparse.ArgumentTypeError("expected true/false")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch full Polymarket Data API trade history into a participation-compatible CSV."
    )
    p.add_argument(
        "--user",
        default=os.getenv("FUNDER_ADDRESS", "").strip(),
        help="Profile / proxy wallet (default: FUNDER_ADDRESS from .env)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output CSV (default: exports/trades_<prefix>_<date>.csv)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Page size (1–10000, default {DEFAULT_LIMIT})",
    )
    p.add_argument(
        "--start",
        type=int,
        default=FULL_HISTORY_START,
        help="Window start epoch seconds (default 1 = full user history)",
    )
    p.add_argument(
        "--end",
        type=int,
        default=None,
        help="Window end epoch seconds (default: now / API default)",
    )
    p.add_argument(
        "--taker-only",
        default=None,
        type=_parse_bool_arg,
        help="true/false. Omit for API default (true). false includes maker fills.",
    )
    p.add_argument("--side", choices=("BUY", "SELL"), default=None, help="Optional side filter")
    p.add_argument("--market", default=None, help="Optional conditionId filter")
    p.add_argument("--sleep", type=float, default=0.25, help="Seconds between page requests")
    p.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    p.add_argument("--max-windows", type=int, default=500, help="Safety cap on time-window loops")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    user = (args.user or "").strip()
    if not user:
        print("error: --user or FUNDER_ADDRESS is required", file=sys.stderr)
        return 2
    if not (1 <= args.limit <= 10_000):
        print("error: --limit must be 1–10000", file=sys.stderr)
        return 2
    out = args.out or default_out_path(user)
    session = make_session()
    stats = fetch_all_trades(
        session,
        user=user,
        out_path=out,
        limit=args.limit,
        start=args.start,
        end=args.end,
        taker_only=args.taker_only,
        side=args.side,
        market=args.market,
        sleep_s=args.sleep,
        timeout=args.timeout,
        max_windows=args.max_windows,
    )
    summary = summarize_csv(out)
    print(json.dumps({"fetch": stats, "file": summary}, indent=2, default=str))
    print(
        f"done: {summary['rows']} rows ({summary['buys']} buys / {summary['sells']} sells) "
        f"{summary['oldest']} → {summary['newest']} → {out}"
    )
    print(f"participation: python check_participation.py --hours 72 --csv {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
