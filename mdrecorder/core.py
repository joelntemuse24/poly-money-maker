"""Recorder core: stream registry, health heartbeats, status file."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import time

import aiohttp

from .common import make_record
from .streams import (
    BINANCE_FUT_URLS,
    BINANCE_SPOT_URLS,
    BinanceStream,
    BitstampStream,
    BybitPerpStream,
    BybitSpotStream,
    ChainlinkStream,
    CoinbaseStream,
    DeribitStream,
    KrakenStream,
    OkxSpotStream,
    OkxSwapStream,
    PolyDiscovery,
    PolyStream,
    WsStream,
)
from .writer import Writer

log = logging.getLogger("mdrecorder")

HEALTH_S = 30.0
DEFAULT_MAX_GB = 12.0
STATUS_PERIOD_S = 30.0


def _default_max_gb():
    raw = os.environ.get("MDREC_MAX_GB", "")
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MAX_GB


class Recorder:
    def __init__(self, out, max_gb=None, queue_size=200_000, only=None):
        self.out = os.path.abspath(out)
        gb = _default_max_gb() if max_gb is None else max_gb
        self.cap_bytes = int(gb * 1_000_000_000) if gb > 0 else 0
        # None records every stream; a set limits recording to those names.
        self.only = only
        # Thread-safe: the writer drains it from a dedicated compression thread.
        self.queue = queue.Queue(maxsize=queue_size)
        self.stop_evt = asyncio.Event()
        self.stopping = False
        self.started_wall = time.time_ns()
        self.started_mono = time.monotonic()
        self.streams = {}
        self.drops = 0
        self.writer = Writer(self)
        self.session = None
        self.discovery = None
        self.poly = None
        self.status_path = os.path.join(self.out, "status.json")
        self.last_trim = None
        self._tasks = []
        self._health_task = None
        self._status_task = None
        self._disk_task = None

    def _enabled(self, name):
        return self.only is None or name in self.only

    def register(self, name):
        self.streams[name] = {
            "msgs": 0,
            "last_msg_mono": None,
            "last_msg_wall": None,
            "reconnects": 0,
            "up": False,
            "errors": 0,
            "last_error": None,
        }

    def stream_up(self, name):
        state = self.streams.get(name)
        if state is not None and not state["up"]:
            state["up"] = True

    def stream_down(self, name):
        state = self.streams.get(name)
        if state is not None:
            state["up"] = False

    def stream_reconnect(self, name):
        state = self.streams.get(name)
        if state is not None:
            state["reconnects"] += 1

    def stream_error(self, name, message):
        state = self.streams.get(name)
        if state is not None:
            state["errors"] += 1
            state["last_error"] = message[:300]

    def discovery_error(self, message):
        self.stream_error("poly_markets", message)

    def write(self, stream, record):
        state = self.streams.get(stream)
        if state is not None:
            state["msgs"] += 1
            state["last_msg_mono"] = record["ts_recv_mono"]
            state["last_msg_wall"] = record["ts_recv_wall"]
            state["up"] = True
        if self.only is not None and stream not in self.only and stream != "_health":
            return
        try:
            self.queue.put_nowait((stream, record))
        except queue.Full:
            self.drops += 1

    def gap(self, stream, down_ns, reason):
        tw, tm = time.time_ns(), time.monotonic_ns()
        rec = make_record("_gap", tw, tm, None)
        rec["stream"] = stream
        rec["down_ns"] = int(down_ns)
        rec["reason"] = str(reason)[:300]
        self.write("_health", rec)

    def note_trim(self, deleted, total):
        tw, tm = time.time_ns(), time.monotonic_ns()
        rec = make_record("_trim", tw, tm, None)
        rec["deleted"] = len(deleted)
        rec["paths"] = deleted
        rec["total_bytes"] = total
        self.write("_health", rec)
        log.warning("disk cap: deleted %d day dirs, now %d bytes", len(deleted), total)

    def status_snapshot(self):
        now_mono = time.monotonic_ns()
        now_wall = time.time_ns()
        streams = {}
        for name, state in self.streams.items():
            last = state["last_msg_mono"]
            streams[name] = {
                "msgs": state["msgs"],
                "last_msg_age_s": None if last is None else round((now_mono - last) / 1e9, 3),
                "reconnects": state["reconnects"],
                "errors": state["errors"],
                "up": state["up"],
                "last_error": state["last_error"],
            }
        try:
            disk_bytes = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _dn, fns in os.walk(self.out)
                for f in fns
            )
        except OSError:
            disk_bytes = None
        markets = {}
        if self.discovery is not None:
            for slug, meta in self.discovery.markets.items():
                markets[slug] = {
                    "conditionId": meta.get("conditionId"),
                    "clobTokenIds": meta.get("clobTokenIds"),
                    "endDate": meta.get("endDate"),
                }
        return {
            "ts": now_wall,
            "pid": os.getpid(),
            "out": self.out,
            "started_wall": self.started_wall,
            "uptime_s": round(time.monotonic() - self.started_mono, 1),
            "cap_bytes": self.cap_bytes,
            "disk_bytes": disk_bytes,
            "queue": self.queue.qsize(),
            "drops": self.drops,
            "records_written": self.writer.records,
            "encode_errors": self.writer.encode_errors,
            "streams": streams,
            "markets": markets,
            "last_trim": self.last_trim,
        }

    def write_status(self):
        snap = self.status_snapshot()
        os.makedirs(self.out, exist_ok=True)
        tmp = self.status_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.status_path)
        except OSError as exc:
            log.warning("status write failed: %s", exc)
        return snap

    async def _health_loop(self):
        while not self.stopping:
            await self._sleep(HEALTH_S)
            if self.stopping:
                break
            tw, tm = time.time_ns(), time.monotonic_ns()
            snap = self.status_snapshot()
            rec = make_record("_health", tw, tm, None)
            rec["streams"] = snap["streams"]
            rec["queue"] = snap["queue"]
            rec["drops"] = snap["drops"]
            rec["records_written"] = snap["records_written"]
            rec["disk_bytes"] = snap["disk_bytes"]
            rec["cap_bytes"] = snap["cap_bytes"]
            self.write("_health", rec)

    async def _status_loop(self):
        while not self.stopping:
            try:
                self.write_status()
            except Exception as exc:
                log.warning("status snapshot failed: %s", exc)
            await self._sleep(STATUS_PERIOD_S)

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self.stop_evt.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def start(self):
        os.makedirs(self.out, exist_ok=True)
        self.writer.start()
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "mdrecorder/1.0"},
            timeout=aiohttp.ClientTimeout(total=15),
        )
        self.discovery = PolyDiscovery(self, self.session)

        candidates = [
            BinanceStream(self, "binance_spot", BINANCE_SPOT_URLS),
            BinanceStream(self, "binance_fut", BINANCE_FUT_URLS),
            CoinbaseStream(self),
            BybitPerpStream(self),
            BybitSpotStream(self),
            OkxSwapStream(self),
            OkxSpotStream(self),
            KrakenStream(self),
            DeribitStream(self),
            BitstampStream(self),
            ChainlinkStream(self),
        ]
        workers = [w for w in candidates if self._enabled(w.name)]
        poly_on = self._enabled("poly_book") or self._enabled("poly_trades")
        if poly_on:
            self.poly = PolyStream(self, self.discovery)
            workers.append(self.poly)
        wanted = {w.name for w in workers if self._enabled(w.name)}
        if poly_on and self._enabled("poly_trades"):
            wanted.add("poly_trades")
        if poly_on or self._enabled("poly_markets"):
            wanted.add("poly_markets")
        wanted.add("_health")
        for name in sorted(wanted):
            self.register(name)

        self._tasks = [
            asyncio.create_task(w.run(), name=f"stream-{w.name}") for w in workers
        ]
        poly_dep = poly_on or self._enabled("poly_markets")
        if poly_dep:
            self._tasks.append(
                asyncio.create_task(self.discovery.discovery_loop(), name="discovery")
            )
        if poly_on:
            self._tasks.append(
                asyncio.create_task(self.discovery.poll_loop(), name="data-api")
            )
        self._health_task = asyncio.create_task(self._health_loop(), name="health")
        self._status_task = asyncio.create_task(self._status_loop(), name="status")
        self.write_status()
        log.info("recording to %s (cap %s bytes)", self.out, self.cap_bytes)

    async def run_until_stopped(self):
        await self.start()
        try:
            await self.stop_evt.wait()
        finally:
            await self.shutdown()

    async def shutdown(self):
        if self.stopping:
            return
        self.stopping = True
        self.stop_evt.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for task in (self._health_task, self._status_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.session is not None:
            await self.session.close()
        # The writer drains the queue before it exits.
        await self.writer.stop()
        self.write_status()
        log.info("stopped: %d records, %d drops", self.writer.records, self.drops)
