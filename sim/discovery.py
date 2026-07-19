"""Discover live BTC 5-minute markets and fetch public order books.

Read-only public HTTP. No auth. No order placement.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import CLOB_HOST, GAMMA_API, DEFAULT_SERIES_SLUG

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "poly-money-maker-shadow-sim/1.1"})

# Process-local discovery cache (reduces Gamma load when co-running with bot)
_discover_cache: Dict[str, Any] = {"ts": 0.0, "markets": [], "series": ""}


@dataclass
class Market:
    condition_id: str
    slug: str
    question: str
    end_ts: float  # unix seconds
    up_token: str
    dn_token: str
    closed: bool = False
    series_slug: str = ""

    def seconds_left(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return self.end_ts - now


@dataclass
class BookSnap:
    token_id: str
    best_bid: Optional[float]
    best_bid_size: float
    best_ask: Optional[float]
    best_ask_size: float
    bids: List[dict] = field(default_factory=list)
    asks: List[dict] = field(default_factory=list)
    ok: bool = True
    error: str = ""


def _parse_end_ts(end: str) -> float:
    return datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()


def _parse_json_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        return json.loads(val)
    return []


def _events_to_markets(
    events: list,
    *,
    series_slug: str,
    now: float,
    horizon_min: float,
    lookback_min: float,
) -> List[Market]:
    out: List[Market] = []
    for ev in events or []:
        markets = ev.get("markets") or []
        if not markets:
            continue
        m = markets[0]
        end = m.get("endDate") or ev.get("endDate")
        if not end:
            continue
        try:
            end_ts = _parse_end_ts(end)
        except Exception:
            continue
        mins = (end_ts - now) / 60.0
        if mins > horizon_min or mins < -lookback_min:
            continue

        tokens = _parse_json_list(m.get("clobTokenIds"))
        outcomes = [str(x).lower() for x in _parse_json_list(m.get("outcomes"))]
        if len(tokens) < 2:
            continue

        up_token = dn_token = None
        if outcomes and len(outcomes) >= 2:
            for i, o in enumerate(outcomes):
                if o == "up":
                    up_token = tokens[i]
                elif o == "down":
                    dn_token = tokens[i]
        if not up_token or not dn_token:
            up_token, dn_token = tokens[0], tokens[1]

        cond = m.get("conditionId") or m.get("condition_id") or ""
        if not cond:
            continue
        out.append(
            Market(
                condition_id=str(cond),
                slug=str(ev.get("slug") or m.get("slug") or cond[:12]),
                question=str(ev.get("title") or m.get("question") or ""),
                end_ts=float(end_ts),
                up_token=str(up_token),
                dn_token=str(dn_token),
                closed=bool(m.get("closed") or ev.get("closed")),
                series_slug=series_slug,
            )
        )
    return out


def discover_btc_markets(
    *,
    series_slug: str = DEFAULT_SERIES_SLUG,
    series_slugs: Optional[List[str]] = None,
    horizon_min: float = 35.0,
    lookback_min: float = 2.0,
    limit: int = 80,
    cache_s: float = 0.0,
) -> List[Market]:
    """Return active BTC up/down markets for one or more series.

    series_slug examples:
      - btc-up-or-down-5m
      - btc-up-or-down-15m
      - btc-up-or-down-hourly
    Pass series_slugs=["btc-up-or-down-15m","btc-up-or-down-hourly"] to combine.
    """
    now = time.time()
    if series_slugs:
        slugs = [str(s).strip() for s in series_slugs if str(s).strip()]
    else:
        slugs = [str(series_slug or DEFAULT_SERIES_SLUG).strip()]
    if not slugs:
        slugs = [DEFAULT_SERIES_SLUG]

    cache_key = "|".join(slugs)
    if (
        cache_s > 0
        and _discover_cache["markets"]
        and _discover_cache.get("series") == cache_key
        and (now - float(_discover_cache["ts"])) < cache_s
    ):
        cached: List[Market] = _discover_cache["markets"]
        return [
            m
            for m in cached
            if -lookback_min <= m.seconds_left(now) / 60.0 <= horizon_min
        ]

    out: List[Market] = []
    seen: set = set()
    for slug in slugs:
        url = f"{GAMMA_API}/events"
        params = {
            "series_slug": slug,
            "active": "true",
            "closed": "false",
            "limit": str(limit),
        }
        r = SESSION.get(url, params=params, timeout=20)
        r.raise_for_status()
        events = r.json()
        for mkt in _events_to_markets(
            events,
            series_slug=slug,
            now=now,
            horizon_min=horizon_min,
            lookback_min=lookback_min,
        ):
            if mkt.condition_id in seen:
                continue
            seen.add(mkt.condition_id)
            out.append(mkt)

    out.sort(key=lambda m: m.end_ts)
    _discover_cache["ts"] = now
    _discover_cache["markets"] = out
    _discover_cache["series"] = cache_key
    return out




def fetch_book(token_id: str, timeout: float = 8.0) -> BookSnap:
    try:
        r = SESSION.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=timeout,
        )
        r.raise_for_status()
        book = r.json()
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = best_bid_sz = None
        best_ask = best_ask_sz = None
        if bids:
            b = max(bids, key=lambda x: float(x.get("price", 0)))
            best_bid = float(b.get("price", 0))
            best_bid_sz = float(b.get("size", 0))
        if asks:
            a = min(asks, key=lambda x: float(x.get("price", 1e9)))
            best_ask = float(a.get("price", 0))
            best_ask_sz = float(a.get("size", 0))
        return BookSnap(
            token_id=token_id,
            best_bid=best_bid,
            best_bid_size=best_bid_sz or 0.0,
            best_ask=best_ask,
            best_ask_size=best_ask_sz or 0.0,
            bids=bids,
            asks=asks,
            ok=True,
        )
    except Exception as e:
        return BookSnap(
            token_id=token_id,
            best_bid=None,
            best_bid_size=0.0,
            best_ask=None,
            best_ask_size=0.0,
            ok=False,
            error=str(e),
        )


def fetch_books_parallel(
    token_ids: List[str],
    max_workers: int = 6,
) -> Dict[str, BookSnap]:
    out: Dict[str, BookSnap] = {}
    if not token_ids:
        return out
    workers = max(1, min(int(max_workers), len(token_ids), 8))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_book, t): t for t in token_ids}
        for fut in as_completed(futs):
            snap = fut.result()
            out[snap.token_id] = snap
    return out


def pair_books(market: Market, books: Dict[str, BookSnap]) -> Tuple[BookSnap, BookSnap]:
    up = books.get(market.up_token) or BookSnap(
        market.up_token, None, 0.0, None, 0.0, ok=False, error="missing"
    )
    dn = books.get(market.dn_token) or BookSnap(
        market.dn_token, None, 0.0, None, 0.0, ok=False, error="missing"
    )
    return up, dn


def discover_btc_5m(**kwargs) -> List[Market]:
    """Backward-compatible alias; prefer discover_btc_markets."""
    kwargs.setdefault("series_slug", "btc-up-or-down-5m")
    return discover_btc_markets(**kwargs)
