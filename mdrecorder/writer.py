"""Async queue, hourly gzip writer, and disk cap for mdrecorder."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import queue
import shutil
import threading
import time

from .common import is_day_dir, tree_bytes, utc_day

FLUSH_S = 5.0
TRIM_S = 60.0
BUFFER_BYTES = 1_000_000
BATCH = 500


class _Sink:
    """One append-only gzip member for a stream-hour file."""

    def __init__(self, path):
        self.path = path
        self._f = open(path, "ab", buffering=0)
        self._gz = gzip.GzipFile(fileobj=self._f, mode="wb", compresslevel=6, mtime=0)
        self._buf = bytearray()

    def write(self, data):
        self._buf += data
        if len(self._buf) >= BUFFER_BYTES:
            self.flush()

    def flush(self):
        if self._buf:
            self._gz.write(bytes(self._buf))
            self._buf.clear()
        self._gz.flush()
        self._f.flush()

    def close(self):
        try:
            self.flush()
        finally:
            try:
                self._gz.close()
            finally:
                self._f.close()


class Writer:
    """Drains the record queue on a dedicated thread.

    JSON encoding and gzip compression stay off the event loop; the loop
    only does an unbounded-in-practice ``put_nowait`` per record.
    """

    def __init__(self, recorder):
        self.rec = recorder
        self.q = recorder.queue
        self._sinks = {}
        self._keys = {}
        self._period = None
        self._thread = None
        self.records = 0
        self.encode_errors = 0

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="mdrecorder-writer", daemon=True
        )
        self._thread.start()

    def _sink(self, stream, day, hour):
        key = (stream, day, hour)
        sink = self._sinks.get(key)
        if sink is None:
            path = os.path.join(self.rec.out, stream, day, f"{stream}-{hour}.jsonl.gz")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            sink = _Sink(path)
            self._sinks[key] = sink
            self._keys[key] = stream
        return sink

    def _write(self, item):
        stream, rec = item
        try:
            line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            self.encode_errors += 1
            return
        secs = rec["ts_recv_wall"] / 1_000_000_000
        tm = time.gmtime(secs)
        day = time.strftime("%Y-%m-%d", tm)
        hour = time.strftime("%Y%m%dT%H", tm)
        if (day, hour) != self._period:
            self._period = (day, hour)
            self.close_stale(day, hour)
        self._sink(stream, day, hour).write(line + b"\n")
        self.records += 1

    def flush_all(self):
        for sink in list(self._sinks.values()):
            try:
                sink.flush()
            except OSError:
                pass

    def close_all(self):
        for sink in list(self._sinks.values()):
            try:
                sink.close()
            except OSError:
                pass
        self._sinks.clear()
        self._keys.clear()

    def _trim(self):
        """Delete oldest day dirs while OUT is over the cap."""
        cap = self.rec.cap_bytes
        if cap <= 0 or not os.path.isdir(self.rec.out):
            return
        today = utc_day()
        days = []
        for stream in os.listdir(self.rec.out):
            sdir = os.path.join(self.rec.out, stream)
            if not os.path.isdir(sdir):
                continue
            for name in os.listdir(sdir):
                if not is_day_dir(name) or name >= today:
                    continue
                path = os.path.join(sdir, name)
                if os.path.isdir(path):
                    days.append((name, path, tree_bytes(path)))
        total = tree_bytes(self.rec.out)
        if total <= cap:
            return
        days.sort(key=lambda row: (row[0], row[1]))
        deleted = []
        for _day, path, size in days:
            if total <= cap:
                break
            shutil.rmtree(path, ignore_errors=True)
            deleted.append(path)
            total -= size
        if not deleted:
            return
        for path in deleted:
            for key in list(self._sinks):
                sink = self._sinks[key]
                if sink.path.startswith(path + os.sep):
                    try:
                        sink.close()
                    except OSError:
                        pass
                    del self._sinks[key]
                    self._keys.pop(key, None)
        self.rec.note_trim(deleted, tree_bytes(self.rec.out))

    def close_stale(self, day, hour):
        """Close hour files that have rolled over so old handles do not leak."""
        for key in list(self._sinks):
            if key[1:] != (day, hour):
                try:
                    self._sinks[key].close()
                except OSError:
                    pass
                del self._sinks[key]
                self._keys.pop(key, None)

    def _run(self):
        last_flush = time.monotonic()
        last_trim = time.monotonic()
        try:
            self._trim()
            while True:
                stopping = self.rec.stopping
                if stopping and self.q.empty():
                    break
                try:
                    item = self.q.get(timeout=0.1 if stopping else FLUSH_S)
                except queue.Empty:
                    if self.rec.stopping:
                        break
                    self.flush_all()
                    last_flush = time.monotonic()
                    if last_flush - last_trim >= TRIM_S:
                        last_trim = last_flush
                        self._trim()
                    continue
                self._write(item)
                for _ in range(BATCH):
                    try:
                        item = self.q.get_nowait()
                    except queue.Empty:
                        break
                    self._write(item)
                now = time.monotonic()
                if now - last_flush >= FLUSH_S:
                    self.flush_all()
                    last_flush = now
                if now - last_trim >= TRIM_S:
                    last_trim = now
                    self._trim()
        finally:
            self.flush_all()
            self.close_all()

    async def stop(self):
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join)
