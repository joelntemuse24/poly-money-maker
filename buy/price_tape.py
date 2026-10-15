"""Last-six-minutes price tape. Logging only.

One compact JSON line per sell-loop tick while a bag's time-to-close is
inside ``price_tape_window_s`` (default 360). The sell thread only queues
the row. A daemon writer appends it to ``logs/price_tape.jsonl`` and rolls
the file at 50MB the same way the oracle tape does. A failure is swallowed
and never changes an order.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Optional

from buy.oracle_log import append_jsonl


PRICE_TAPE_MAX_BYTES = 50 * 1024 * 1024
PRICE_TAPE_WINDOW_S = 360.0
_QUEUE_MAX = 4096
_FLUSH = object()

_path = Path("logs/price_tape.jsonl")
_max_bytes = PRICE_TAPE_MAX_BYTES
_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()
_flush_event = threading.Event()


def set_log_path(path: Any) -> None:
    """Point the writer at ``path``. The sell loop sets this once."""
    global _path
    try:
        _path = Path(path)
    except Exception:
        return


def set_max_bytes(max_bytes: int) -> None:
    """Size cap before the live file is archived. Tests use a tiny cap."""
    global _max_bytes
    try:
        _max_bytes = int(max_bytes)
    except (TypeError, ValueError):
        return


def enabled(cfg: Any) -> bool:
    """``price_tape_enabled`` defaults true. Only an explicit false is off."""
    if not isinstance(cfg, dict) or "price_tape_enabled" not in cfg:
        return True
    raw = cfg.get("price_tape_enabled")
    if raw is None or raw == "":
        return True
    return bool(raw)


def window_s(cfg: Any) -> float:
    """``price_tape_window_s`` defaults to 360. A bad value keeps that default.

    An explicit 0 leaves the tape quiet. Positive is the last N seconds.
    """
    raw = PRICE_TAPE_WINDOW_S
    if isinstance(cfg, dict) and "price_tape_window_s" in cfg:
        raw = cfg.get("price_tape_window_s")
        if raw is None or raw == "":
            return PRICE_TAPE_WINDOW_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return PRICE_TAPE_WINDOW_S
    if value != value or value < 0 or value == float("inf"):
        return PRICE_TAPE_WINDOW_S
    return value


def tape_due(ttm_s: Any, window: Any, *, enabled_flag: bool = True) -> bool:
    """True while the window is still open and ttm is within ``window``."""
    if not enabled_flag:
        return False
    try:
        ttm = float(ttm_s)
        span = float(window)
    except (TypeError, ValueError):
        return False
    if ttm != ttm or span != span:
        return False
    if ttm <= 0 or span <= 0 or ttm == float("inf") or span == float("inf"):
        return False
    return ttm <= span


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _px(value: Any) -> Optional[float]:
    out = _num(value)
    if out is None:
        return None
    return round(out, 4)


def _sz(value: Any) -> Optional[float]:
    out = _num(value)
    if out is None:
        return None
    return round(out, 4)


def price_tape_row(
    *,
    ts: Any,
    slug: Any,
    condition_id: Any,
    ttm_s: Any,
    up_bid: Any,
    up_ask: Any,
    dn_bid: Any,
    dn_ask: Any,
    up_bid_sz: Any = None,
    up_ask_sz: Any = None,
    dn_bid_sz: Any = None,
    dn_ask_sz: Any = None,
    oracle: Any = None,
) -> dict:
    """One tick. Prices already fetched; oracle is an in-memory mark or empty."""
    mark = oracle if isinstance(oracle, dict) else {}
    ttm = _num(ttm_s)
    return {
        "ts": _num(ts),
        "slug": None if slug is None else str(slug),
        "condition_id": None if condition_id is None else str(condition_id),
        "ttm_s": None if ttm is None else round(ttm, 3),
        "up_bid": _px(up_bid),
        "up_ask": _px(up_ask),
        "dn_bid": _px(dn_bid),
        "dn_ask": _px(dn_ask),
        "up_bid_sz": _sz(up_bid_sz),
        "up_ask_sz": _sz(up_ask_sz),
        "dn_bid_sz": _sz(dn_bid_sz),
        "dn_ask_sz": _sz(dn_ask_sz),
        "oracle_px": mark.get("oracle_px"),
        "oracle_start": mark.get("oracle_start"),
        "oracle_dist_usd": mark.get("oracle_dist_usd"),
    }


def _ensure_thread() -> None:
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_worker, name="price-tape", daemon=True)
        _thread.start()


def _worker() -> None:
    while True:
        item = _queue.get()
        try:
            if item is _FLUSH:
                continue
            append_jsonl(_path, item, max_bytes=_max_bytes)
        except Exception:
            pass
        finally:
            if item is _FLUSH:
                _flush_event.set()


def note(**fields: Any) -> None:
    """Queue one tape line. Never raises and never blocks the sell thread."""
    try:
        row = price_tape_row(**fields)
        _ensure_thread()
        _queue.put_nowait(row)
    except Exception:
        return


def flush(timeout: float = 2.0) -> None:
    """Wait until lines queued so far are written. Tests only."""
    try:
        if _thread is None or not _thread.is_alive():
            return
        _flush_event.clear()
        _queue.put(_FLUSH, timeout=timeout)
        _flush_event.wait(timeout)
    except Exception:
        return
