"""Append-only gzip JSONL, one file per bag.

Each flush writes a complete gzip member and fsyncs it. A crash loses
only the open buffer (at most ``flush_bytes`` or ``flush_s``). Members
concatenate, so a later reader can ``gzip.open`` the whole file.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


def tape_path(root: Path | str, slug: str, start_ts: float) -> Path:
    """``root/YYYY-MM-DD/<slug>.jsonl.gz`` using the window start, UTC."""
    try:
        moment = float(start_ts)
    except (TypeError, ValueError):
        moment = 0.0
    if moment <= 0:
        moment = time.time()
    day = datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d")
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(slug))[:180]
    if not safe:
        safe = "unknown"
    return Path(root) / day / f"{safe}.jsonl.gz"


class TapeWriter:
    """Small per-slug buffers. The lock is held only around the buffer."""

    def __init__(
        self,
        root: Path | str,
        *,
        flush_bytes: int = 32_768,
        flush_s: float = 1.0,
        compresslevel: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = Path(root)
        self.flush_bytes = max(1, int(flush_bytes))
        self.flush_s = max(0.05, float(flush_s))
        self.compresslevel = int(compresslevel)
        self._clock = clock
        self._lock = threading.Lock()
        self._files: dict[Path, object] = {}
        self._buf: dict[Path, list[bytes]] = {}
        self._buf_bytes: dict[Path, int] = {}
        self._last: dict[Path, float] = {}
        self._synced: dict[Path, float] = {}
        self.rows = 0
        self.closed = False

    def write(self, record: dict) -> None:
        slug = str(record.get("slug") or "")
        if not slug:
            return
        path = tape_path(self.root, slug, float(record.get("start_ts") or 0.0))
        line = json.dumps(record, separators=(",", ":"), default=str).encode("utf-8") + b"\n"
        with self._lock:
            if self.closed:
                return
            bucket = self._buf.setdefault(path, [])
            bucket.append(line)
            self._buf_bytes[path] = self._buf_bytes.get(path, 0) + len(line)
            self._last.setdefault(path, self._clock())
            self.rows += 1
            if self._buf_bytes[path] >= self.flush_bytes:
                self._flush_path(path)

    def flush(self, force: bool = False) -> None:
        with self._lock:
            if self.closed:
                return
            now = self._clock()
            for path in list(self._buf):
                if not self._buf.get(path):
                    continue
                if force or (now - self._last.get(path, now)) >= self.flush_s:
                    self._flush_path(path)

    def close(self) -> None:
        with self._lock:
            self.closed = True
            for path in list(self._buf):
                if self._buf.get(path):
                    self._flush_path(path, sync=True)
            for handle in self._files.values():
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError:
                    pass
                try:
                    handle.close()
                except OSError:
                    pass
            self._files.clear()

    def _flush_path(self, path: Path, *, sync: bool | None = None) -> None:
        lines = self._buf.get(path) or []
        if not lines:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._files.get(path)
        if handle is None:
            handle = open(path, "ab", buffering=0)
            self._files[path] = handle
        gz = gzip.GzipFile(fileobj=handle, mode="wb", compresslevel=self.compresslevel, mtime=0)
        try:
            for line in lines:
                gz.write(line)
        finally:
            gz.close()
        handle.flush()
        now = self._clock()
        do_sync = (now - self._synced.get(path, 0.0) >= self.flush_s) if sync is None else sync
        if do_sync:
            os.fsync(handle.fileno())
            self._synced[path] = now
        self._buf[path] = []
        self._buf_bytes[path] = 0
        self._last[path] = now
