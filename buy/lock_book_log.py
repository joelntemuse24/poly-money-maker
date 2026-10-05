"""Optional JSONL tape of the live BTC 5m/15m order books.

Off by default. The websocket apply thread only calls ``on_touch``. When the
logger is disabled that call returns on its first line. When it is enabled,
``on_touch`` adds each touched token to a small bounded queue (one slot per
token, a full queue drops and counts). A daemon writer thread reads the
materialized book, applies the throttle, formats one compact row, and appends
it to ``book_log_path``. Sorting, formatting, and file I/O never run on the
apply thread.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from buy.log_archive import roll_if_over

QUEUE_MAX = 256
BATCH_MAX = 256


def _as_int(value: Any, default: int) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < 1 or parsed != int(parsed):
        return default
    return int(parsed)


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


def _levels(rows: Any, n: int) -> list:
    return [[row[0], round(float(row[1]), 4)] for row in rows[:n]]


class BookSnapshotLogger:
    def __init__(
        self,
        root: Path,
        book_fn: Callable[[str], Optional[dict]],
        *,
        clock: Callable[[], float] = time.time,
        threaded: bool = True,
    ) -> None:
        self._root = Path(root)
        self._book_fn = book_fn
        self._clock = clock
        self._threaded = threaded
        self.enabled = False
        self._levels_n = 5
        self._interval_s = 0.1
        self._max_bytes = 0
        self._path = self._root / "logs" / "books.jsonl"
        self._meta: dict[str, tuple] = {}
        self._queued: set[str] = set()
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._last: dict[str, tuple] = {}
        self._fh: Any = None
        self._fh_path: Optional[Path] = None
        self._size = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.rows = 0
        self.drops = 0
        self.errors = 0
        self.last_error = ""

    def configure(self, cfg: dict) -> None:
        """Apply the ``book_log_*`` keys. Safe to call on every config reload."""
        enabled = cfg.get("book_log_enabled", False)
        if not isinstance(enabled, bool):
            enabled = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
        self._levels_n = _as_int(cfg.get("book_log_levels"), 5)
        self._interval_s = _as_float(cfg.get("book_log_min_interval_ms"), 100.0) / 1000.0
        self._max_bytes = int(_as_float(cfg.get("book_log_max_bytes"), 0.0))
        raw_path = cfg.get("book_log_path")
        self._path = self._root / (str(raw_path) if raw_path else "logs/books.jsonl")
        self.enabled = enabled
        if enabled:
            self._ensure_thread()

    def set_markets(self, markets: Iterable[Any]) -> None:
        """Register token -> (slug, asset, duration, side). Disabled is a no-op."""
        if not self.enabled:
            return
        meta: dict[str, tuple] = {}
        for market in markets:
            meta[str(market.up_token)] = (market.slug, market.asset, market.duration, "up")
            meta[str(market.dn_token)] = (market.slug, market.asset, market.duration, "down")
        self._meta = meta
        self._last = {token: row for token, row in self._last.items() if token in meta}

    def on_touch(self, tokens: Iterable[str], now: float = 0.0) -> None:
        """Book feed hook. Runs on the websocket apply thread."""
        if not self.enabled:
            return
        meta = self._meta
        queued = self._queued
        for token in tokens:
            if token in queued or token not in meta:
                continue
            queued.add(token)
            try:
                self._q.put_nowait(token)
            except queue.Full:
                queued.discard(token)
                self.drops += 1

    def stats(self) -> dict:
        return {"rows": self.rows, "drops": self.drops, "errors": self.errors, "queued": self._q.qsize()}

    def pump(self) -> int:
        """Write everything queued now. The writer thread calls this; tests call it directly."""
        tokens: list[str] = []
        while len(tokens) < BATCH_MAX:
            try:
                tokens.append(self._q.get_nowait())
            except queue.Empty:
                break
        return self._write_tokens(tokens)

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._close_file()

    def _ensure_thread(self) -> None:
        if not self._threaded or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lockbot-book-log", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            tokens = [first]
            while len(tokens) < BATCH_MAX:
                try:
                    tokens.append(self._q.get_nowait())
                except queue.Empty:
                    break
            self._write_tokens(tokens)

    def _write_tokens(self, tokens: list[str]) -> int:
        wrote = 0
        try:
            for token in tokens:
                # Clear first so a touch during formatting queues the token again.
                self._queued.discard(token)
                line = self._row(token)
                if line is not None:
                    self._append(line)
                    wrote += 1
            if self._fh is not None:
                self._fh.flush()
        except Exception as exc:
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:160]
            self._close_file()
        return wrote

    def _row(self, token: str) -> Optional[bytes]:
        if not self.enabled:
            return None
        meta = self._meta.get(token)
        if meta is None:
            return None
        book = self._book_fn(token)
        if not book:
            return None
        asks = book.get("asks") or []
        bids = book.get("bids") or []
        recv = book.get("recv_ts")
        best_ask = asks[0][0] if asks else None
        best_bid = bids[0][0] if bids else None
        now = float(self._clock())
        last = self._last.get(token)
        if last is not None:
            if recv == last[3]:
                return None
            if now - last[0] < self._interval_s and best_ask == last[1] and best_bid == last[2]:
                return None
        self._last[token] = (now, best_ask, best_bid, recv)
        n = self._levels_n
        row = {
            "ts": round(now, 3),
            "recv_ts": recv,
            "token_id": token,
            "slug": meta[0],
            "asset": meta[1],
            "duration": meta[2],
            "side": meta[3],
            "best_ask": best_ask,
            "best_bid": best_bid,
            "ask_levels": _levels(asks, n),
            "bid_levels": _levels(bids, n),
        }
        return (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")

    def _append(self, data: bytes) -> None:
        path = self._path
        if self._fh is not None and self._fh_path != path:
            self._close_file()
        if self._fh is None:
            self._open(path)
        cap = self._max_bytes
        if cap > 0 and self._size > 0 and self._size + len(data) >= cap:
            self._close_file()
            roll_if_over(path, len(data), cap)
            self._open(path)
        self._fh.write(data)
        self._size += len(data)
        self.rows += 1

    def _open(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._size = path.stat().st_size
        except OSError:
            self._size = 0
        self._fh = open(path, "ab")
        self._fh_path = path

    def _close_file(self) -> None:
        fh, self._fh, self._fh_path = self._fh, None, None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
