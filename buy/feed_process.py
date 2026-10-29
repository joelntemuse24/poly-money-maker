"""Feed process: websockets + jump detection in a child, order sending in the parent.

Everything here is inert unless ``feed_process`` is on. The shape:

* **Child** (``FeedWorker``, runs in a ``fork``ed process): owns every feed
  websocket (``buy.binance_trades.BinanceTrades`` with all extra URLs), the
  ``PriceGrid``, the jump detector and the feed health bookkeeping. It writes
  every accepted tick into a shared tick ring and, on a qualifying jump, one
  fixed-size trigger record, then wakes the parent with ``os.eventfd``.
* **Parent** (``FeedProcessClient``): a dedicated thread blocks on that eventfd
  and calls the sniper as soon as a trigger lands. Presigning, the HTTP/2 warm
  connection and the FAK POST stay in this process.

Why a shared ring for ticks and not a message queue: the parent still needs the
grid for ``twap60``/``price_at``/``latest_exchange_ms`` (the offset proxy and the
book tape). Relaying ticks through a pipe would add per-tick IPC and a second
PriceGrid; instead the child's ring *is* the grid state and the parent reads it
directly (``SharedPriceGrid``), so no copy and no relay thread.

``feed_process`` off (the default) leaves the in-process feeds in place. If the
child dies or stops heartbeating the parent logs ``feed_process_fallback``,
starts in-process feeds and keeps trading on the old path.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import threading
import time
from multiprocessing import shared_memory
from multiprocessing.reduction import DupFd
from typing import Any, Callable, Optional

MAGIC = 0x506F6C79_46656431  # "PolyFed1"
VERSION = 1
MAX_SOURCES = 9          # primary + binance_fut + 6 extras, plus headroom
RING_CAPACITY = 4096
TRIGGER_SLOTS = 32
TRIGGER_FIELDS = 16
HEARTBEAT_S = 1.0
STALL_FACTOR = 4.0

# Header slots (int64).
H_MAGIC, H_VERSION, H_STOP, H_TICKS, H_TRIG_W, H_TRIG_R, H_HEARTBEAT_NS = range(7)
H_SOURCE_COUNT = 7
HEADER_SLOTS = 32

# Trigger record layout (float64), read by the parent under the slot's seq.
T_SEQ, T_EXCH_MS, T_RECV_WALL_MS, T_SPOT, T_RAW_SPOT, T_MIN, T_MAX = range(7)
T_UP_JUMP, T_DOWN_JUMP, T_SOURCE, T_END_TS = 7, 8, 9, 10
T_RULE_KEY, T_STATUS = 11, 12  # 14, 15 reserved

SOURCE_NAMES = (
    "primary", "binance_spot", "binance_fut", "bybit", "coinbase",
    "deribit", "kraken", "bitstamp", "other",
)


def source_slot(name: str) -> int:
    try:
        return SOURCE_NAMES.index(str(name))
    except ValueError:
        return len(SOURCE_NAMES) - 1


class Layout:
    """Byte offsets inside the shared block."""

    def __init__(self, sources: int = MAX_SOURCES, capacity: int = RING_CAPACITY,
                 triggers: int = TRIGGER_SLOTS):
        # Never fewer slots than named sources: slot ids come from SOURCE_NAMES and
        # two feeds sharing a slot would silently see each other's ticks.
        self.sources = max(len(SOURCE_NAMES), int(sources))
        self.capacity = max(2, int(capacity))
        self.triggers = max(2, int(triggers))
        self.header_bytes = HEADER_SLOTS * 8
        self.trigger_offset = self.header_bytes
        self.trigger_bytes = self.triggers * (TRIGGER_FIELDS + 1) * 8  # +1: seq guard
        self.tick_offset = self.trigger_offset + self.trigger_bytes
        # Two axes per source: exchange time and local receive time. Each axis is
        # ``capacity`` timestamps followed by ``capacity`` prices. The strategy's
        # staleness gates read receive time, so both must reach the parent.
        self.per_source_doubles = 2 * self.capacity
        self.axis_doubles = self.sources * self.per_source_doubles
        self.total_bytes = self.tick_offset + 2 * self.axis_doubles * 8

    def _axis_base(self, slot: int, axis: int) -> int:
        return ((self.tick_offset // 8) + int(axis) * self.axis_doubles
                + slot * self.per_source_doubles)

    def ts_index(self, slot: int, position: int, axis: int = 0) -> int:
        return self._axis_base(slot, axis) + (position % self.capacity)

    def px_index(self, slot: int, position: int, axis: int = 0) -> int:
        return self._axis_base(slot, axis) + self.capacity + (position % self.capacity)

    def count_index(self, slot: int, axis: int = 0) -> int:
        """Header slot holding the tick count for one source/axis."""
        return H_TICKS + int(axis) * len(SOURCE_NAMES) + slot

    def header_index(self, slot: int) -> int:
        return slot

    def trigger_index(self, slot: int) -> int:
        return (self.trigger_offset // 8) + (slot % self.triggers) * (TRIGGER_FIELDS + 1)


class SharedBlock:
    """A ``ctypes`` int64/float64 view over a ``shared_memory`` segment."""

    def __init__(self, size: int, *, create: bool, name: Optional[str] = None):
        self._shm = (shared_memory.SharedMemory(create=True, size=max(size, 4096), name=name)
                     if create else shared_memory.SharedMemory(name=name))
        if not create:
            # Attaching also registers the segment with the multiprocessing
            # resource tracker. Only the parent owns the lifetime (it unlinks on
            # stop); left registered here, the child would try to remove the live
            # segment when it exits.
            try:
                from multiprocessing import resource_tracker

                resource_tracker.unregister(self._shm._name, "shared_memory")
            except Exception:
                pass
        self.name = self._shm.name
        self.size = size
        self.i64 = (ctypes.c_int64 * (size // 8)).from_buffer(self._shm.buf)
        self.f64 = (ctypes.c_double * (size // 8)).from_buffer(self._shm.buf)

    def close(self) -> None:
        # Drop the ctypes views first: they export pointers into the mmap and
        # keep SharedMemory from closing.
        try:
            del self.i64
            del self.f64
        except AttributeError:
            pass
        try:
            self._shm.close()
        except Exception:
            pass

    def unlink(self) -> None:
        # Unlink the segment we created. ``SharedMemory.unlink`` also drops it from
        # the multiprocessing resource tracker, so it must not be unregistered
        # separately - a second unregister raises from the tracker's own thread.
        try:
            self._shm.unlink()
        except Exception:
            pass


class TickRingWriter:
    """Single-producer ring append: prices then the count, so a reader never sees a torn pair."""

    def __init__(self, block: SharedBlock, layout: Layout, *, axis: int = 0):
        self.block, self.layout = block, layout
        self.axis = int(axis)

    def slot(self, source: str) -> int:
        return source_slot(source)

    def append(self, source: str, exchange_ms: float, price: float) -> None:
        slot = self.slot(source)
        count_slot = self.layout.header_index(self.layout.count_index(slot, self.axis))
        position = self.block.i64[count_slot]
        self.block.f64[self.layout.px_index(slot, position, self.axis)] = price
        self.block.f64[self.layout.ts_index(slot, position, self.axis)] = exchange_ms
        self.block.i64[count_slot] = position + 1


class TickRingReader:
    """Reader side of :class:`TickRingWriter`; tolerates an in-flight append."""

    def __init__(self, block: SharedBlock, layout: Layout, *, axis: int = 0):
        self.block, self.layout = block, layout
        self.axis = int(axis)

    def slot(self, source: str) -> int:
        return source_slot(source)

    def count(self, source: str) -> int:
        return int(self.block.i64[self.layout.header_index(
            self.layout.count_index(self.slot(source), self.axis))])

    def latest(self, source: str) -> Optional[tuple[float, float]]:
        slot = self.slot(source)
        total = self.count(source)
        if total <= 0:
            return None
        position = total - 1
        return (self.block.f64[self.layout.ts_index(slot, position, self.axis)],
                self.block.f64[self.layout.px_index(slot, position, self.axis)])

    def window_minmax(self, source: str, lower: float, upper: float):
        slot = self.slot(source)
        total = self.count(source)
        if total <= 0:
            return None
        lowest, highest = math.inf, -math.inf
        span = min(total, self.layout.capacity)
        for back in range(span):
            position = total - 1 - back
            ts = self.block.f64[self.layout.ts_index(slot, position, self.axis)]
            if ts < lower:
                break
            if ts > upper:
                continue
            px = self.block.f64[self.layout.px_index(slot, position, self.axis)]
            if px < lowest:
                lowest = px
            if px > highest:
                highest = px
        if lowest is math.inf:
            return None
        return (lowest, highest)

    def between(self, source: str, lower: float, upper: float) -> list[tuple[float, float]]:
        slot = self.slot(source)
        total = self.count(source)
        out: list[tuple[float, float]] = []
        for back in range(min(total, self.layout.capacity)):
            position = total - 1 - back
            ts = self.block.f64[self.layout.ts_index(slot, position, self.axis)]
            if ts < lower:
                break
            if ts <= upper:
                out.append((ts, self.block.f64[self.layout.px_index(slot, position, self.axis)]))
        return out


def write_trigger(block: SharedBlock, layout: Layout, *, exchange_ms: float, recv_wall_ms: float,
                  spot: float, raw_spot: float, low: float, high: float, up_jump: float,
                  down_jump: float, source: str, end_ts: Optional[float] = None) -> None:
    """Publish one trigger and bump the write counter (single producer)."""
    index = int(block.i64[layout.header_index(H_TRIG_W)])
    base = layout.trigger_index(index)
    block.f64[base + T_SEQ] = float(index)
    block.f64[base + T_EXCH_MS] = float(exchange_ms)
    block.f64[base + T_RECV_WALL_MS] = float(recv_wall_ms)
    block.f64[base + T_SPOT] = float(spot)
    block.f64[base + T_RAW_SPOT] = float(raw_spot)
    block.f64[base + T_MIN] = float(low)
    block.f64[base + T_MAX] = float(high)
    block.f64[base + T_UP_JUMP] = float(up_jump)
    block.f64[base + T_DOWN_JUMP] = float(down_jump)
    block.f64[base + T_SOURCE] = float(source_slot(source))
    block.f64[base + T_END_TS] = float(end_ts or 0.0)
    block.f64[base + T_STATUS] = 1.0
    block.i64[layout.header_index(H_TRIG_W)] = index + 1


def read_trigger(block: SharedBlock, layout: Layout, index: int) -> Optional[dict]:
    base = layout.trigger_index(index)
    if block.f64[base + T_STATUS] != 1.0:
        return None
    return {
        "seq": int(block.f64[base + T_SEQ]),
        "ts_ms": block.f64[base + T_EXCH_MS],
        "recv_wall_ms": block.f64[base + T_RECV_WALL_MS],
        "spot": block.f64[base + T_SPOT],
        "raw_spot": block.f64[base + T_RAW_SPOT],
        "min": block.f64[base + T_MIN],
        "max": block.f64[base + T_MAX],
        "up_jump": block.f64[base + T_UP_JUMP],
        "down_jump": block.f64[base + T_DOWN_JUMP],
        "source": SOURCE_NAMES[int(block.f64[base + T_SOURCE]) % len(SOURCE_NAMES)],
        "end_ts": block.f64[base + T_END_TS] or None,
    }


class SharedPriceGrid:
    """The parent's read-only ``PriceGrid`` over the child's tick ring.

    Implements the handful of reads the 5m strategy performs (``twap60``,
    ``price_at``, ``latest_exchange_ms``, ``latest_recv_wall_ms``, and the window
    scans). ``best_jump_in_window`` returns the detector's own answer, so no
    jump is recomputed in this process.
    """

    #: The parent must not run its own jump scan: the child's verdict is the only
    #: fresh one, and a poll-time rescan here would act on a stale trigger.
    trigger_only = True

    def __init__(self, block: SharedBlock, layout: Layout):
        self.block, self.layout = block, layout
        self.ring = TickRingReader(block, layout, axis=0)       # exchange time
        self.ring_recv = TickRingReader(block, layout, axis=1)  # local receive time
        self._latest_trigger: Optional[dict] = None
        self._trigger_lock = threading.Lock()

    def set_trigger(self, trigger: dict) -> None:
        with self._trigger_lock:
            self._latest_trigger = dict(trigger)

    def latest_trigger(self) -> Optional[dict]:
        with self._trigger_lock:
            return None if self._latest_trigger is None else dict(self._latest_trigger)

    def latest_exchange_ms(self) -> Optional[float]:
        latest = self.ring.latest("primary")
        return None if latest is None else float(latest[0])

    def latest_recv_ms(self, source: str = "primary") -> Optional[float]:
        """Local receive stamp (epoch ms) of the newest tick from ``source``."""
        latest = self.ring_recv.latest(source)
        return None if latest is None else float(latest[0])

    def latest_recv_wall_ms(self, source: str = "primary") -> Optional[float]:
        return self.latest_recv_ms(source)

    def recv_extrema(self, window_ms: float, *, source: str = "primary",
                     ref_ms: Optional[float] = None):
        """``window_extrema`` on the receive-time axis, as the live grid computes it."""
        latest = self.ring_recv.latest(source)
        if latest is None:
            return None
        latest_ms, spot = latest
        spot_now = self.ring.latest(source)
        ref = latest_ms if ref_ms is None else float(ref_ms)
        bounds = self.ring_recv.window_minmax(source, ref - float(window_ms), ref)
        if bounds is None:
            return None
        return ((spot_now[1] if spot_now is not None else spot), bounds[0], bounds[1], latest_ms)

    def price_at(self, g: int) -> Optional[float]:
        """Newest price at or before 100ms bucket ``g`` (deque-free scan)."""
        target = int(g) * 100.0
        rows = self.ring.between("primary", target - 100.0 * self.layout.capacity, target)
        if not rows:
            return None
        return rows[0][1]

    def twap60(self, g_end: int) -> Optional[float]:
        target = int(g_end)
        values = []
        for step in range(60):
            value = None
            wanted = target - 10 * step
            rows = self.ring.between("primary", (wanted - 600) * 100.0, wanted * 100.0)
            for ts, px in rows:
                value = px
                break
            if value is None:
                return None
            values.append(value)
        return sum(values) / 60.0

    def window_extrema(self, window_ms: float = 500.0, now_ms: Optional[float] = None,
                       *, source: str = "primary"):
        latest = self.ring.latest(source)
        if latest is None:
            return None
        latest_ms, spot_now = latest
        ref_ms = latest_ms if now_ms is None else float(now_ms)
        bounds = self.ring.window_minmax(source, ref_ms - float(window_ms), ref_ms)
        if bounds is None:
            return None
        return (spot_now, bounds[0], bounds[1], latest_ms)

    def best_jump_in_window(self, window_ms: float = 500.0, now_ms: Optional[float] = None,
                            *, recv_now_ms: Optional[float] = None) -> Optional[dict]:
        return self.latest_trigger()

    def jump_in_window(self, window_ms: float = 500.0, now_ms: Optional[float] = None,
                       *, source: str = "primary") -> Optional[dict]:
        return self.latest_trigger()

    def feed_recv_snapshot(self, window_ms: float, ref_ms: Optional[float] = None) -> dict:
        """Per-feed recv stamps for the latency journal, from the ring."""
        latest = self.latest_trigger()
        if latest is None:
            return {"feed_recv": {}, "first_feed": None}
        return {"feed_recv": {latest["source"]: latest["recv_wall_ms"]},
                "first_feed": latest["source"]}

    def feed_health(self) -> dict[str, dict[str, Any]]:
        return {src: {"connected": self.ring.count(src) > 0}
                for src in SOURCE_NAMES if self.ring.count(src) > 0}


def detect_trigger(grid, *, threshold_usd: float, window_ms: float,
                   extra_enabled: bool = True) -> Optional[dict]:
    """Run the same jump scan the in-process path runs; ``None`` below the threshold."""
    best_fn = getattr(grid, "best_jump_in_window", None) if extra_enabled else None
    jump_fn = getattr(grid, "jump_in_window", None)
    info = best_fn(float(window_ms)) if callable(best_fn) else None
    if info is None and callable(jump_fn):
        info = jump_fn(float(window_ms))
    if info is None:
        return None
    if float(info["up_jump"]) < threshold_usd and float(info["down_jump"]) < threshold_usd:
        return None
    return info


class FeedWorker:
    """The child: feeds + detector + shared-ring publisher."""

    def __init__(self, cfg: dict, block: SharedBlock, layout: Layout, wake_fd: Optional[int],
                 *, now: Callable[[], float] = time.time):
        self.cfg, self.block, self.layout, self.wake_fd = cfg, block, layout, wake_fd
        self.now = now
        self.ring = TickRingWriter(block, layout, axis=0)
        # A parallel ring keyed on the local receive stamp: the strategy's staleness
        # gates read receive time, not exchange time, so the parent needs both.
        self.ring_recv = TickRingWriter(block, layout, axis=1)
        self.grid = None
        self.feeds = None
        self.stop = threading.Event()
        self._last_heartbeat = 0.0
        self._triggers = 0

    def _on_event(self, event: str, **fields: Any) -> None:
        # The child has no journal; these are the events an operator needs.
        if event in ("fivemin_binance_connect", "fivemin_binance_disconnect"):
            self._stderr(f"{event} {fields.get('feed')} {fields.get('reason') or ''}")

    @staticmethod
    def _stderr(text: str) -> None:
        try:
            os.write(2, (str(text)[:300] + "\n").encode("utf-8", "replace"))
        except OSError:
            pass

    def _on_error(self, error: str) -> None:
        self._stderr(f"feed_process_error {str(error)[:240]}")

    def install_mirror(self) -> None:
        """Wrap ``grid.add`` so every feed thread's tick is mirrored into the ring.

        Called once, before the feeds start. Each accepted tick is then mirrored
        exactly once (``_on_tick`` must not write to the ring itself) and the
        parent reads the real per-source history on both axes. The ring write is a
        few stores, and the reader side takes no lock.
        """
        original_add = self.grid.add

        def add_and_mirror(exchange_ms, price, *, source="primary", recv_ms=None, recv_wall_ms=None):
            result = original_add(exchange_ms, price, source=source, recv_ms=recv_ms,
                                  recv_wall_ms=recv_wall_ms)
            try:
                self.ring.append(source, float(exchange_ms), float(price))
                if recv_wall_ms is not None:
                    self.ring_recv.append(source, float(recv_wall_ms), float(price))
            except Exception:
                pass
            return result

        self.grid.add = add_and_mirror

    def _on_tick(self, exchange_ms: float, price: float) -> None:
        """One accepted primary tick, on the feed thread.

        ``BinanceTrades`` has already called ``grid.add`` for this tick (through
        the ring-mirroring wrapper installed in :meth:`run`), so the only work
        left here is the jump scan. It runs on this thread because that is the
        lowest-latency place to detect: the tick is in the grid and nothing has
        to be handed anywhere.
        """
        if self.stop.is_set():
            return
        self._maybe_trigger()

    def _maybe_trigger(self) -> None:
        threshold = float(self.cfg.get("sniper_jump_usd", 25.0)) - 1e-9
        info = detect_trigger(
            self.grid, threshold_usd=threshold,
            window_ms=float(self.cfg.get("sniper_window_ms", 500)),
            extra_enabled=bool(self.cfg.get("sniper_extra_ws_enabled", True)),
        )
        if info is None:
            return
        recv_wall = info.get("recv_wall_ms")
        if recv_wall is None:
            latest = self.ring_recv.latest(str(info.get("source") or "primary"))
            recv_wall = latest[0] if latest is not None else self.now() * 1000.0
        write_trigger(
            self.block, self.layout,
            exchange_ms=float(info.get("ts_ms") or 0.0),
            recv_wall_ms=float(recv_wall),
            spot=float(info["spot"]), raw_spot=float(info.get("raw_spot", info["spot"])),
            low=float(info["min"]), high=float(info["max"]),
            up_jump=float(info["up_jump"]), down_jump=float(info["down_jump"]),
            source=str(info.get("source") or "primary"),
        )
        self._triggers += 1
        self._wake()

    def _wake(self) -> None:
        if self.wake_fd is None:
            return
        try:
            os.write(self.wake_fd, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        except OSError:
            pass

    def _heartbeat(self) -> None:
        now = self.now()
        if now - self._last_heartbeat < HEARTBEAT_S:
            return
        self._last_heartbeat = now
        self.block.i64[self.layout.header_index(H_HEARTBEAT_NS)] = int(now * 1e9)

    def run(self) -> int:
        from buy.binance_trades import BinanceTrades, PriceGrid

        self.block.i64[self.layout.header_index(H_MAGIC)] = MAGIC
        self.block.i64[self.layout.header_index(H_VERSION)] = VERSION
        if self.cfg.get("_feed_process_fake_feed"):
            return self._run_fake()
        self.grid = PriceGrid(use_rust=bool(self.cfg.get("rust_hotpath", False)))
        extra_urls = list(self.cfg.get("sniper_extra_ws_urls") or []) \
            if self.cfg.get("sniper_extra_ws_enabled", True) else []
        if self.cfg.get("sniper_futures_ws_enabled") and self.cfg.get("sniper_futures_ws_url"):
            extra_urls.append(str(self.cfg["sniper_futures_ws_url"]))
        self.install_mirror()
        self.feeds = BinanceTrades(
            self.grid, url=str(self.cfg.get("fivemin_binance_url") or ""),
            on_error=self._on_error, on_event=self._on_event,
            on_tick=self._on_tick, extra_urls=extra_urls,
        )
        self.feeds.start()
        try:
            while not self.stop.is_set() and not self.block.i64[self.layout.header_index(H_STOP)]:
                self._heartbeat()
                self.stop.wait(0.05)
        finally:
            self.feeds.stop()
        return 0


    def _run_fake(self) -> int:
        """Offline feed for the benchmark and tests: replay a scripted tick list.

        The ticks go through ``grid.add`` and then ``_on_tick``, exactly as a
        websocket thread's do, so ring writes, detection and the wakeup are
        identical; only ``recv()`` is replaced by a sleep.
        """
        from buy.binance_trades import PriceGrid

        self.grid = PriceGrid(use_rust=False)
        self.install_mirror()
        for entry in self.cfg.get("_feed_process_fake_ticks") or []:
            if self.stop.is_set():
                break
            exchange_ms, price, delay = (list(entry) + [0.0])[:3]
            if delay:
                self.stop.wait(float(delay))
            if self.stop.is_set():
                break
            ms, px = float(exchange_ms), float(price)
            self.grid.add(ms, px, source="primary", recv_ms=ms,
                          recv_wall_ms=self.now() * 1000.0)
            self._on_tick(ms, px)
            self._heartbeat()
        while not self.stop.is_set() and not self.block.i64[self.layout.header_index(H_STOP)]:
            self._heartbeat()
            self.stop.wait(0.02)
        return 0


def child_main(cfg: dict, shm_name: str, layout: Layout, wake_fd: Any) -> int:
    """Entry point for the ``spawn``ed child.

    ``wake_fd`` is a ``DupFd`` under spawn (and a plain int under fork), so accept
    both and detach the resource wrapper here.
    """
    if hasattr(wake_fd, "detach"):
        wake_fd = wake_fd.detach()
    block = SharedBlock(layout.total_bytes, create=False, name=shm_name)
    try:
        return FeedWorker(cfg, block, layout, wake_fd).run()
    finally:
        block.close()


class FeedProcessClient:
    """Parent side: own the child, wait on the eventfd, expose the shared grid."""

    def __init__(self, cfg: dict, *, on_trigger: Callable[[dict], None],
                 log: Callable[..., None] = lambda *a, **k: None,
                 on_fallback: Optional[Callable[[str], None]] = None,
                 layout: Optional[Layout] = None, now: Callable[[], float] = time.time):
        self.cfg, self.on_trigger, self.log = cfg, on_trigger, log
        self.on_fallback = on_fallback
        self.now = now
        self.layout = layout or Layout()
        self.block: Optional[SharedBlock] = None
        self.grid: Optional[SharedPriceGrid] = None
        self.process = None
        self.wake_fd: Optional[int] = None
        self.wake_w: Optional[int] = None
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._monitor: Optional[threading.Thread] = None
        self._last_seq = 0
        self.fell_back = False
        self.triggers = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> bool:
        import multiprocessing as mp

        try:
            self.block = SharedBlock(self.layout.total_bytes, create=True)
        except Exception as exc:
            self.log("feed_process_shm_fail", error=str(exc)[:200])
            return False
        self.block.i64[self.layout.header_index(H_STOP)] = 0
        self.block.i64[self.layout.header_index(H_TRIG_W)] = 0
        self.wake_fd = self.wake_w = None
        if hasattr(os, "eventfd"):
            try:
                # Blocking on purpose: the trigger thread must sleep in the kernel
                # and be woken by the counter, not spin on a poll interval. A
                # non-blocking fd would add its sleep to every trigger.
                self.wake_fd = os.eventfd(0)
                self.wake_w = self.wake_fd
            except OSError:
                self.wake_fd = self.wake_w = None
        self.grid = SharedPriceGrid(self.block, self.layout)
        # A plain subprocess, not a ``multiprocessing`` child: by the time this
        # runs the bot has ~20 threads (sell, mint, oracle, redeem, 8 feeds, log
        # listener). Forking that can deadlock the child on an inherited lock
        # (looks exactly like a feed stall), and spawn would re-import mintbot.py
        # with all its module-level clients. ``buy.feed_worker`` imports only the
        # feed modules, and the fd is handed over with DupFd.
        try:
            self.process = self._spawn()
        except Exception as exc:
            self.log("feed_process_start_fail", error=str(exc)[:200])
            self._teardown_block()
            return False
        if self.wake_fd is not None:
            self._reader = threading.Thread(target=self._read_loop, name="mintbot-feed-trigger",
                                            daemon=True)
            self._reader.start()
        else:
            self._reader = threading.Thread(target=self._poll_loop, name="mintbot-feed-trigger",
                                            daemon=True)
            self._reader.start()
        self._monitor = threading.Thread(target=self._monitor_loop, name="mintbot-feed-health",
                                         daemon=True)
        self._monitor.start()
        self.log("feed_process_started", shm=self.block.name, wakeup="eventfd" if self.wake_fd is not None else "poll")
        return True

    def _spawn(self):
        """Start ``buy.feed_worker``; the shm name and fds go on argv, config on stdin."""
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        wake = self.wake_fd if self.wake_fd is not None else -1
        argv = [
            sys.executable, "-m", "buy.feed_worker",
            self.block.name, str(self.layout.sources), str(self.layout.capacity),
            str(self.layout.triggers), str(wake),
            str(float(self.cfg.get("feed_process_status_s", 5.0) or 5.0)),
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        payload = json.dumps({k: v for k, v in self.cfg.items()
                              if isinstance(v, (str, int, float, bool, list, type(None)))},
                             default=str)
        process = subprocess.Popen(
            argv, cwd=str(root), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=((wake,) if wake >= 0 else ()),
        )
        try:
            process.stdin.write(payload.encode("utf-8"))
            process.stdin.close()
        except Exception:
            pass
        return process

    def alive(self) -> bool:
        return bool(self.process is not None and self.process.poll() is None)

    def stop(self) -> None:
        self._stop.set()
        if self.block is not None:
            try:
                self.block.i64[self.layout.header_index(H_STOP)] = 1
            except Exception:
                pass
        if self.wake_fd is not None:
            try:
                os.write(self.wake_fd, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
        if self.process is not None:
            try:
                self.process.wait(timeout=5.0)
            except Exception:
                pass
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2.0)
                except Exception:
                    self.process.kill()
        for thread in (self._reader, self._monitor):
            if thread is not None:
                thread.join(timeout=2.0)
        if self.wake_fd is not None:
            try:
                os.close(self.wake_fd)
            except OSError:
                pass
        self._teardown_block()

    def _teardown_block(self) -> None:
        if self.block is not None:
            self.block.close()
            self.block.unlink()
            self.block = None

    # -- trigger delivery ------------------------------------------------
    def _dispatch(self) -> None:
        if self.block is None:
            return
        written = int(self.block.i64[self.layout.header_index(H_TRIG_W)])
        while self._last_seq < written:
            record = read_trigger(self.block, self.layout, self._last_seq)
            self._last_seq += 1
            if record is None:
                continue
            self.triggers += 1
            if self.grid is not None:
                self.grid.set_trigger(record)
            try:
                self.on_trigger(record)
            except Exception as exc:
                self.log("feed_process_trigger_fail", error=str(exc)[:200])

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                os.read(self.wake_fd, 8)
            except InterruptedError:
                continue
            except OSError:
                return
            self._dispatch()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._dispatch()
            self._stop.wait(0.002)

    def _monitor_loop(self) -> None:
        period = max(0.05, float(self.cfg.get("feed_process_status_s", 5.0) or 5.0))
        while not self._stop.is_set():
            self._stop.wait(period)
            if self._stop.is_set():
                break
            reason = self._health_failure()
            if reason is None:
                continue
            if not self.fell_back:
                self.fell_back = True
                self.log("feed_process_fallback", reason=reason,
                         triggers=self.triggers, pid=None if self.process is None else self.process.pid)
                if self.on_fallback is not None:
                    try:
                        self.on_fallback(reason)
                    except Exception:
                        pass
            return

    def _health_failure(self) -> Optional[str]:
        if self.process is None:
            return "no_process"
        if self.process.poll() is not None:
            return f"exitcode:{self.process.returncode}"
        if self.block is None:
            return "no_shm"
        beat_ns = int(self.block.i64[self.layout.header_index(H_HEARTBEAT_NS)])
        if beat_ns <= 0:
            return None
        age = self.now() - beat_ns / 1e9
        period = max(0.05, float(self.cfg.get("feed_process_status_s", 5.0) or 5.0))
        if age > max(period * STALL_FACTOR, HEARTBEAT_S * STALL_FACTOR):
            return f"heartbeat_stale:{age:.1f}s"
        return None


def trigger_to_jump_info(trigger: dict) -> dict:
    """The ``best_jump_in_window`` dict for a trigger record."""
    return {
        "spot": trigger["spot"],
        "raw_spot": trigger.get("raw_spot", trigger["spot"]),
        "min": trigger["min"],
        "max": trigger["max"],
        "up_jump": trigger["up_jump"],
        "down_jump": trigger["down_jump"],
        "ts_ms": trigger["ts_ms"],
        "recv_ms": trigger["ts_ms"],
        "recv_wall_ms": trigger["recv_wall_ms"],
        "source": trigger["source"],
    }
