"""Reconstruct a bag's book from a recorded tape and walk it."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from recorder.parse import apply_record, empty_book, price_key


@dataclass(frozen=True)
class FillEstimate:
    """A market order walking the displayed book. No queue and no latency."""

    filled: float
    remaining: float
    vwap: Optional[float]
    notional: float
    levels: tuple[tuple[float, float], ...]


def iter_records(path: Path | str) -> Iterator[dict]:
    """Yield tape rows. Gzip members may be concatenated."""
    file_path = Path(path)
    opener = gzip.open if file_path.name.endswith(".gz") else open
    with opener(file_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _stamp(record: dict, clock: str) -> Optional[float]:
    key = "exch_ts" if clock == "exch" else "recv_ts"
    value = record.get(key)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed


def _dir_day(path: Path) -> Optional[datetime]:
    try:
        return datetime.strptime(path.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_float(value: object) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if parsed != parsed or parsed <= 0:
        return 0.0
    return parsed


def crosses_midnight(records: Iterable[dict], file_day: datetime) -> bool:
    """True when a 15m window stored under ``file_day`` reaches the next UTC day."""
    next_midnight = file_day.timestamp() + 86400.0
    for record in records:
        start = _as_float(record.get("start_ts"))
        end = _as_float(record.get("end_ts"))
        if start > 0 and start < next_midnight and start + 900.0 >= next_midnight:
            return True
        if end >= next_midnight and (start <= 0 or start < next_midnight):
            return True
    return False


class Tape:
    """One bag file. Each query replays rows up to ``ts`` from the start."""

    def __init__(self, records: Iterable[dict]) -> None:
        self.records = list(records)

    @classmethod
    def load(cls, path: Path | str) -> "Tape":
        file_path = Path(path)
        bag_rows = list(iter_records(file_path))
        if file_path.name == "oracle.jsonl.gz":
            return cls(bag_rows)
        rows = list(bag_rows)
        sibling = file_path.with_name("oracle.jsonl.gz")
        if sibling.is_file():
            rows.extend(iter_records(sibling))
        file_day = _dir_day(file_path.parent)
        # The bag's own window decides. The shared oracle file also holds
        # later bags from the same UTC day, and those must not pull in tomorrow.
        if file_day is not None and crosses_midnight(bag_rows, file_day):
            nxt = file_path.parent.parent / (file_day + timedelta(days=1)).strftime("%Y-%m-%d") / "oracle.jsonl.gz"
            if nxt.is_file():
                rows.extend(iter_records(nxt))
        return cls(rows)

    def book_at(self, ts: float, *, leg: str = "up", clock: str = "recv") -> dict[str, dict[float, float]]:
        moment = float(ts)
        books: dict[str, dict[str, dict[float, float]]] = {}
        indexed: list[tuple[float, int, dict]] = []
        for index, record in enumerate(self.records):
            if str(record.get("leg") or "") != leg:
                continue
            stamp = _stamp(record, clock)
            if stamp is None or stamp > moment:
                continue
            indexed.append((stamp, index, record))
        indexed.sort(key=lambda item: (item[0], item[1]))
        for _stamp_s, _index, record in indexed:
            apply_record(books, record)
        return books.get(leg) or empty_book()

    def depth_at(
        self,
        ts: float,
        side: str,
        price: float,
        *,
        leg: str = "up",
        clock: str = "recv",
    ) -> float:
        """Shares at or better than ``price``.

        Bids: level price >= ``price``. Asks: level price <= ``price``.
        """
        book_side = _side(side)
        book = self.book_at(ts, leg=leg, clock=clock)
        limit = float(price)
        total = 0.0
        for level, size in book[book_side].items():
            if book_side == "bid" and level + 1e-12 >= limit:
                total += size
            elif book_side == "ask" and level <= limit + 1e-12:
                total += size
        return total

    def size_at(
        self,
        ts: float,
        side: str,
        price: float,
        *,
        leg: str = "up",
        clock: str = "recv",
    ) -> float:
        """Shares resting at ``price`` exactly (one rung)."""
        book_side = _side(side)
        book = self.book_at(ts, leg=leg, clock=clock)
        key = price_key(price)
        if key is None:
            return 0.0
        return float(book[book_side].get(key, 0.0))

    def fill_estimate(
        self,
        ts: float,
        side: str,
        shares: float,
        *,
        leg: str = "up",
        clock: str = "recv",
    ) -> FillEstimate:
        """Walk the book for a market order.

        ``sell`` / ``bid`` takes bids from high to low (a market sell).
        ``buy`` / ``ask`` takes asks from low to high.
        """
        try:
            want = float(shares)
        except (TypeError, ValueError):
            want = 0.0
        if want <= 0:
            return FillEstimate(0.0, 0.0, None, 0.0, ())
        action = str(side or "").strip().lower()
        if action in {"sell", "bid"}:
            book_side = "bid"
            reverse = True
        elif action in {"buy", "ask"}:
            book_side = "ask"
            reverse = False
        else:
            raise ValueError("side must be sell, buy, bid, or ask")
        book = self.book_at(ts, leg=leg, clock=clock)
        ladder = sorted(book[book_side].items(), key=lambda item: item[0], reverse=reverse)
        remaining = want
        filled = 0.0
        notional = 0.0
        used: list[tuple[float, float]] = []
        for level, size in ladder:
            if remaining <= 1e-12:
                break
            take = size if size < remaining else remaining
            if take <= 0:
                continue
            filled += take
            notional += take * level
            remaining -= take
            used.append((level, take))
        vwap = (notional / filled) if filled > 0 else None
        return FillEstimate(filled, remaining, vwap, notional, tuple(used))

    def oracle_at(
        self,
        ts: float,
        source: str = "twap",
        *,
        clock: str = "recv",
    ) -> Optional[str]:
        """Latest reference price at or before ``ts`` for ``source``."""
        moment = float(ts)
        found: Optional[tuple[float, int, str]] = None
        for index, record in enumerate(self.records):
            if str(record.get("event_type") or "") != "oracle":
                continue
            if str(record.get("source") or "") != source:
                continue
            stamp = _stamp(record, clock)
            if stamp is None or stamp > moment:
                continue
            price = record.get("price")
            if price is None or price == "":
                continue
            if found is None or (stamp, index) >= (found[0], found[1]):
                found = (stamp, index, str(price))
        return None if found is None else found[2]


def up_bid_scenario(
    tape: Tape,
    ts: float,
    *,
    price: float = 0.92,
    shares: float = 100.0,
    clock: str = "recv",
) -> dict:
    """Bids >= ``price`` on Up, the exact rung, and a market sell of ``shares``."""
    fill = tape.fill_estimate(ts, "sell", shares, leg="up", clock=clock)
    return {
        "ts": float(ts),
        "leg": "up",
        "price": float(price),
        "shares_bid_at_or_above": tape.depth_at(ts, "bid", price, leg="up", clock=clock),
        "shares_bid_at_price": tape.size_at(ts, "bid", price, leg="up", clock=clock),
        "sell_shares": float(shares),
        "sell_filled": fill.filled,
        "sell_remaining": fill.remaining,
        "sell_vwap": fill.vwap,
        "sell_notional": fill.notional,
    }


def _side(side: str) -> str:
    text = str(side or "").strip().lower()
    if text in {"bid", "bids", "buy"}:
        return "bid"
    if text in {"ask", "asks", "sell"}:
        return "ask"
    raise ValueError("side must be bid or ask")
