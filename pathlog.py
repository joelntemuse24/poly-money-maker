#!/usr/bin/env python3
"""Record CLOB top-of-book paths for BTC Up/Down markets.

No orders. One JSONL file per market under pathlog/ticks/. Later,
check_path_backtest.py answers: if we had entered at price X with Y
seconds left, would that leg have won?

Usage:
    python pathlog.py
Kill switch: touch STOP_PATHLOG
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from rich.console import Console

from buy.market import MarketGateway, MintMarket

console = Console()
REPO = Path(__file__).resolve().parent

LOG_FILE = REPO / "pathlog.log"
LOCK_FILE = REPO / ".pathlog.lock"
HEARTBEAT_FILE = REPO / ".heartbeat_pathlog"
STOP_FILE = REPO / "STOP_PATHLOG"
TICK_DIR = REPO / "pathlog" / "ticks"

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

SERIES = [
    "btc-up-or-down-5m",
    "btc-up-or-down-15m",
    "btc-up-or-down-hourly",
]

# Seconds before end to start sampling. Whole 5m window; last 8m of 15m; last 15m of hourly.
RECORD_BEFORE_END_S = {
    "btc-up-or-down-5m": 5 * 60,
    "btc-up-or-down-15m": 8 * 60,
    "btc-up-or-down-hourly": 15 * 60,
}

POLL_S = 1.0
BOOK_WORKERS = 8
RESOLVE_GRACE_S = 20.0

_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    _shutdown = True


def log_setup() -> None:
    import logging

    logger = logging.getLogger("pathlog")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def log_event(event: str, **kwargs: Any) -> None:
    import logging

    payload = {"ts": time.time(), "event": event, **kwargs}
    logging.getLogger("pathlog").info(json.dumps(payload, default=str))


def write_heartbeat(status: str, **fields: Any) -> None:
    payload = {"ts": time.time(), "status": status, **fields}
    temporary = str(HEARTBEAT_FILE) + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
    os.replace(temporary, HEARTBEAT_FILE)


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("[bold red]Another pathlog process already holds the lock.[/]")
        sys.exit(1)
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def tick_path(slug: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in slug)
    return TICK_DIR / f"{safe}.jsonl"


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def file_has_event(path: Path, event: str) -> bool:
    if not path.exists():
        return False
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
                if row.get("e") == event:
                    return True
    except OSError:
        return False
    return False


def _best(levels: Any, side: str) -> Tuple[Optional[float], float]:
    valid: List[Tuple[float, float]] = []
    for level in levels or []:
        if not isinstance(level, dict):
            continue
        try:
            price = float(level.get("price"))
            size = float(level.get("size"))
        except (TypeError, ValueError):
            continue
        if not (0 < price < 1) or size <= 0:
            continue
        valid.append((price, size))
    if not valid:
        return None, 0.0
    if side == "bid":
        price, size = max(valid, key=lambda item: item[0])
    else:
        price, size = min(valid, key=lambda item: item[0])
    return price, size


def fetch_book(token_id: str) -> Tuple[Optional[float], Optional[float]]:
    try:
        response = requests.get(
            f"{CLOB}/book",
            params={"token_id": token_id},
            timeout=5,
            headers={"User-Agent": "poly-money-maker-pathlog/1.0"},
        )
        response.raise_for_status()
        book = response.json()
    except Exception:
        return None, None
    if not isinstance(book, dict):
        return None, None
    bid, _ = _best(book.get("bids"), "bid")
    ask, _ = _best(book.get("asks"), "ask")
    return bid, ask


def sample_market(market: MintMarket, now: float) -> Optional[dict]:
    up_bid, up_ask = fetch_book(market.up_token)
    dn_bid, dn_ask = fetch_book(market.dn_token)
    if up_ask is None and dn_ask is None:
        return None
    return {
        "e": "tick",
        "ts": round(now, 3),
        "ttm": round(market.end_ts - now, 2),
        "ub": up_bid,
        "ua": up_ask,
        "db": dn_bid,
        "da": dn_ask,
    }


def ensure_header(path: Path, market: MintMarket) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    append_jsonl(
        path,
        {
            "e": "open",
            "slug": market.slug,
            "cid": market.condition_id,
            "series": market.series_slug,
            "start": market.start_ts,
            "end": market.end_ts,
            "up": market.up_token,
            "dn": market.dn_token,
            "q": market.question,
        },
    )


def gamma_winner(session: requests.Session, slug: str) -> Optional[str]:
    try:
        response = session.get(
            f"{GAMMA}/markets",
            params={"slug": slug},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    market = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(market, dict):
        return None
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (TypeError, ValueError):
            outcomes = []
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except (TypeError, ValueError):
            prices = []
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    try:
        parsed = [float(p) for p in prices]
    except (TypeError, ValueError):
        return None
    if max(parsed) < 0.99:
        return None
    winner_idx = 0 if parsed[0] >= parsed[1] else 1
    name = str(outcomes[winner_idx]).strip().lower()
    if name in ("up", "down"):
        return name
    return None


def pending_resolve_slugs() -> List[str]:
    if not TICK_DIR.exists():
        return []
    out: List[str] = []
    for path in TICK_DIR.glob("*.jsonl"):
        if file_has_event(path, "resolved"):
            continue
        out.append(path.stem)
    return out


def run_cycle(gateway: MarketGateway, session: requests.Session) -> str:
    now = time.time()
    if STOP_FILE.exists():
        write_heartbeat("stopped")
        return "stopped"

    markets = gateway.discover(SERIES)
    due: List[MintMarket] = []
    for market in markets:
        if market.end_ts <= now or market.start_ts > now:
            continue
        horizon = RECORD_BEFORE_END_S.get(market.series_slug, 5 * 60)
        ttm = market.end_ts - now
        if 0 < ttm <= horizon:
            due.append(market)

    sampled = 0
    if due:
        with ThreadPoolExecutor(max_workers=BOOK_WORKERS) as pool:
            futures = {
                pool.submit(sample_market, market, now): market for market in due
            }
            for future in as_completed(futures):
                market = futures[future]
                try:
                    tick = future.result()
                except Exception as exc:
                    log_event("sample_fail", slug=market.slug, error=str(exc)[:160])
                    continue
                if not tick:
                    continue
                path = tick_path(market.slug)
                ensure_header(path, market)
                append_jsonl(path, tick)
                sampled += 1

    resolved = 0
    for slug in pending_resolve_slugs():
        path = tick_path(slug)
        header = None
        try:
            with open(path, encoding="utf-8") as handle:
                first = handle.readline()
            header = json.loads(first) if first.strip() else None
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(header, dict):
            continue
        end_ts = float(header.get("end") or 0)
        if end_ts <= 0 or now < end_ts + RESOLVE_GRACE_S:
            continue
        winner = gamma_winner(session, str(header.get("slug") or slug))
        if not winner:
            continue
        append_jsonl(
            path,
            {"e": "resolved", "ts": round(now, 3), "winner": winner, "src": "gamma"},
        )
        resolved += 1
        log_event("resolved", slug=header.get("slug") or slug, winner=winner)

    write_heartbeat("ok", markets=len(markets), sampled=sampled, resolved=resolved)
    return "ok"


def main() -> int:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    log_setup()
    lock = acquire_lock()
    TICK_DIR.mkdir(parents=True, exist_ok=True)

    gateway = MarketGateway(
        gamma_url=GAMMA,
        data_api_url="https://data-api.polymarket.com",
        discover_cache_s=8.0,
    )
    session = requests.Session()
    session.headers["User-Agent"] = "poly-money-maker-pathlog/1.0"

    console.print("[bold cyan]pathlog[/] recording CLOB ticks → pathlog/ticks/")
    log_event("startup", series=SERIES, poll_s=POLL_S)

    while not _shutdown:
        if STOP_FILE.exists():
            break
        try:
            status = run_cycle(gateway, session)
            if status == "stopped":
                break
        except Exception as exc:
            log_event("cycle_error", error=str(exc)[:300])
            write_heartbeat("error", error=str(exc)[:120])
        time.sleep(POLL_S)

    console.print("[dim]pathlog stopped[/]")
    try:
        lock.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
