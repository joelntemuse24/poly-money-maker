"""Single-worker, latest-wins persistence for JSON-like trading state."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable


def snapshot_state(value):
    """Detach mutable JSON containers; share immutable scalar values."""
    if isinstance(value, dict):
        return {key: snapshot_state(item) for key, item in value.items()}
    if isinstance(value, list):
        return [snapshot_state(item) for item in value]
    return value


def atomic_write(path: Path, payload: dict) -> int:
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

    return len(data.encode("utf-8"))


class StateWriter:
    """Own submitted snapshots; coalesce only state, retaining every archive row.

    A successful newer snapshot satisfies waits for superseded sequences. Failed
    writes wake waiters with False; a subsequent submit retries with fresh state.
    Callbacks execute on the worker and must not wait for this writer.
    """

    def __init__(self, path, *, archive_path=None, on_saved=None, on_error=None, log=None,
                 write: Callable = atomic_write):
        self.path = Path(path)
        self.archive_path = Path(archive_path) if archive_path else None
        self.on_saved = on_saved
        self.on_error = on_error
        self.log = log or (lambda *_a, **_kw: None)
        self._write = write
        self._condition = threading.Condition()
        self._pending = None
        self._archive = []
        self._seq = 0
        self._saved = 0
        self._finished = 0
        self._stopping = False
        self._late_submit_logged = False
        self.last_error = None
        self._thread = threading.Thread(target=self._run, name="mintbot-state-writer", daemon=True)
        self._thread.start()

    def submit(self, snapshot, *, archive_rows=()) -> int:
        """Take ownership of a detached snapshot; caller must never mutate it."""
        with self._condition:
            if not self._stopping:
                self._seq += 1
                self._pending = (self._seq, snapshot)
                self._archive.extend(archive_rows)
                self._condition.notify_all()
                return self._seq
            warn = not self._late_submit_logged
            self._late_submit_logged = True
        if warn:
            self._log("state_save_after_stop", error="late state submission ignored during shutdown")
        return 0

    def wait(self, seq, timeout=None) -> bool:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._saved >= seq or (self._finished >= seq and self.last_error is not None),
                timeout=timeout,
            )
            return bool(ready and self._saved >= seq)

    def flush(self, timeout=None) -> bool:
        with self._condition:
            seq = self._seq
        return self.wait(seq, timeout)

    def stop(self, timeout=None) -> bool:
        started = time.monotonic()
        with self._condition:
            self._stopping = True
            seq = self._seq
            self._condition.notify_all()
        ok = self.wait(seq, timeout)
        remaining = None if timeout is None else max(0, timeout - (time.monotonic() - started))
        self._thread.join(remaining)
        return ok and not self._thread.is_alive()

    def _append_archive(self, rows):
        if not rows:
            return
        if self.archive_path is None:
            raise ValueError("archive_path is required for pruned state")
        from buy.log_archive import roll_if_over

        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n" for row in rows)
        byte_count = len(data.encode("utf-8"))
        roll_if_over(self.archive_path, incoming_bytes=byte_count, max_bytes=20_000_000)
        with self.archive_path.open("a", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(str(self.archive_path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        rows.clear()
        return byte_count

    def _callback(self, callback, *args):
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            self._log("state_writer_callback_fail", error=str(exc)[:240])

    def _log(self, event, **fields):
        try:
            self.log(event, **fields)
        except Exception:
            pass

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._stopping)
                if self._pending is None:
                    return
                seq, snapshot = self._pending
                self._pending = None
                rows, self._archive = self._archive, []
            try:
                archive_count = len(rows)
                intents_before = len(snapshot.get("intents", {})) + archive_count
                # Archive evidence must reach disk before deleting it from state.
                archived_bytes = self._append_archive(rows) or 0
                rows = []
                after = self._write(self.path, snapshot)
                if archive_count:
                    if after is None:
                        # Injected test writers may not return a byte count.
                        after = len(json.dumps(snapshot, separators=(",", ":"), sort_keys=True).encode("utf-8"))
                    self._log("state_pruned_summary", removed=archive_count,
                              before=intents_before, after=len(snapshot.get("intents", {})),
                              bytes_before=after + archived_bytes, bytes_after=after)
            except Exception as exc:
                first = self.last_error is None
                # Invalidate the submitted digest before waking durable waiters.
                self._callback(self.on_error, seq, exc)
                if first:
                    self._log("state_save_fail", error=str(exc)[:240], seq=seq)
                with self._condition:
                    self.last_error = exc
                    self._archive = rows + self._archive
                    self._finished = seq
                    self._condition.notify_all()
            else:
                self._callback(self.on_saved, seq, snapshot)
                with self._condition:
                    self.last_error = None
                    self._saved = seq
                    self._finished = seq
                    self._condition.notify_all()
