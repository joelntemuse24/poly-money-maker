"""Parse Polymarket CLOB market-channel frames into tape rows.

Pure functions. No sockets and no files. ``book`` replaces a ladder,
``price_change`` sets or deletes one level, and ``last_trade_price``
is stored without changing the book.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional


BOOK_TYPES = frozenset({"book"})
CHANGE_TYPES = frozenset({"price_change"})
TRADE_TYPES = frozenset({"last_trade_price", "last_trade"})


@dataclass(frozen=True)
class TokenRef:
    slug: str
    leg: str
    start_ts: float
    end_ts: float = 0.0


def backoff_s(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Reconnect delay. Doubles from ``base`` and never exceeds ``cap``."""
    try:
        n = int(attempt)
    except (TypeError, ValueError):
        n = 0
    if n < 0:
        n = 0
    # Cap the shift so a huge attempt cannot overflow the float.
    return min(float(cap), float(base) * (2 ** min(n, 16)))


def exchange_ts(value: Any) -> Optional[float]:
    """Exchange stamp in seconds. Milliseconds (values above 1e12) are scaled."""
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    if parsed > 1e12:
        parsed = parsed / 1000.0
    return parsed


def _text(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def _is_control(text: str) -> bool:
    return text.strip().upper() in {"", "PING", "PONG"}


def decode_payload(raw: Any) -> list[dict]:
    """One frame as a list of event dicts. Pings and junk are an empty list."""
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    text = _text(raw).strip()
    if _is_control(text):
        return []
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("event_type") or event.get("type") or "").strip()


def _level_pairs(levels: Any) -> list[list[str]]:
    if not isinstance(levels, list):
        return []
    out: list[list[str]] = []
    for level in levels:
        if isinstance(level, dict):
            price, size = level.get("price"), level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = level[0], level[1]
        else:
            continue
        if price is None or size is None or price == "" or size == "":
            continue
        out.append([str(price), str(size)])
    return out


def _base(event: Mapping[str, Any], ref: TokenRef, recv_ts: float, kind: str) -> dict:
    row = {
        "event_type": kind,
        "recv_ts": float(recv_ts),
        "exch_ts": exchange_ts(event.get("timestamp")),
        "slug": ref.slug,
        "leg": ref.leg,
        "start_ts": float(ref.start_ts),
        "asset_id": str(event.get("asset_id") or ""),
    }
    digest = event.get("hash")
    if digest:
        row["hash"] = str(digest)
    market = event.get("market")
    if market:
        row["market"] = str(market)
    return row


def _lookup(tokens: Mapping[str, TokenRef], asset_id: Any) -> Optional[TokenRef]:
    if asset_id is None or asset_id == "":
        return None
    return tokens.get(str(asset_id))


def records_from_payload(
    raw: Any,
    recv_ts: float,
    tokens: Mapping[str, TokenRef],
) -> list[dict]:
    """Tape rows for one websocket frame. Unknown assets are skipped."""
    rows: list[dict] = []
    for event in decode_payload(raw):
        kind = _event_type(event)
        if kind in BOOK_TYPES:
            ref = _lookup(tokens, event.get("asset_id"))
            if ref is None:
                continue
            row = _base(event, ref, recv_ts, "book")
            row["bids"] = _level_pairs(event.get("bids"))
            row["asks"] = _level_pairs(event.get("asks"))
            rows.append(row)
            continue
        if kind in CHANGE_TYPES:
            changes = event.get("price_changes") or event.get("changes") or []
            if not isinstance(changes, list):
                continue
            frame_ts = exchange_ts(event.get("timestamp"))
            for change in changes:
                if not isinstance(change, dict):
                    continue
                ref = _lookup(tokens, change.get("asset_id") or event.get("asset_id"))
                if ref is None:
                    continue
                row = _base(change, ref, recv_ts, "price_change")
                if row["exch_ts"] is None:
                    row["exch_ts"] = frame_ts
                row["asset_id"] = str(change.get("asset_id") or event.get("asset_id") or "")
                row["price"] = str(change.get("price") if change.get("price") is not None else "")
                row["size"] = str(change.get("size") if change.get("size") is not None else "")
                row["side"] = str(change.get("side") or "")
                rows.append(row)
            continue
        if kind in TRADE_TYPES:
            ref = _lookup(tokens, event.get("asset_id"))
            if ref is None:
                continue
            row = _base(event, ref, recv_ts, "last_trade")
            row["price"] = str(event.get("price") if event.get("price") is not None else "")
            row["size"] = str(event.get("size") if event.get("size") is not None else "")
            row["side"] = str(event.get("side") or "")
            rows.append(row)
    return rows


def price_key(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not 0.0 < parsed < 1.0:
        return None
    return round(parsed, 6)


def size_value(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def ladder_name(side: Any) -> Optional[str]:
    text = str(side or "").strip().upper()
    if text in {"BUY", "BID"}:
        return "bid"
    if text in {"SELL", "ASK"}:
        return "ask"
    return None


def empty_book() -> dict[str, dict[float, float]]:
    return {"bid": {}, "ask": {}}


def apply_record(books: dict[str, dict[str, dict[float, float]]], record: Mapping[str, Any]) -> None:
    """Apply one tape row. ``books`` is keyed by leg (``up`` / ``down``)."""
    kind = str(record.get("event_type") or "")
    leg = str(record.get("leg") or "")
    if not leg:
        return
    if kind == "book":
        book = empty_book()
        for side, field in (("bid", "bids"), ("ask", "asks")):
            for pair in record.get(field) or []:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                px = price_key(pair[0])
                sz = size_value(pair[1])
                if px is None or sz is None or sz <= 0:
                    continue
                book[side][px] = sz
        books[leg] = book
        return
    if kind != "price_change":
        return
    side = ladder_name(record.get("side"))
    px = price_key(record.get("price"))
    sz = size_value(record.get("size"))
    if side is None or px is None or sz is None:
        return
    book = books.get(leg)
    if book is None:
        book = empty_book()
        books[leg] = book
    if sz <= 0:
        book[side].pop(px, None)
    else:
        book[side][px] = sz
