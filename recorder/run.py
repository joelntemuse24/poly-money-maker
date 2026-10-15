"""Recorder process loops. No orders, no mintbot import.

The book socket and the BTC reference socket reconnect on their own.
A failure in one does not exit the process.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from recorder.disk import enforce_tape_cap
from recorder.markets import bags_to_record, market_key
from recorder.oracle_rows import oracle_records, oracle_tape_row
from recorder.parse import TokenRef, backoff_s, records_from_payload
from recorder.tape import TapeWriter


CLOB_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
SERIES = ["btc-up-or-down-15m"]


def _is_timeout(exc: BaseException) -> bool:
    name = type(exc).__name__
    return isinstance(exc, (TimeoutError, socket.timeout)) or "Timeout" in name


def _frame_text(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", "replace").strip()
    return str(raw or "").strip()


def _is_pong(raw: Any) -> bool:
    return _frame_text(raw).upper() == "PONG"


def _is_ping(raw: Any) -> bool:
    return _frame_text(raw).upper() == "PING"


def rss_bytes() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, IndexError, ValueError):
        return 0
    return 0


def log_event(event: str, **fields: Any) -> None:
    payload = {"ts": time.time(), "event": event, **fields}
    try:
        print(json.dumps(payload, default=str), flush=True)
    except Exception:
        return


class MarketState:
    """Latest windows. ``generation`` bumps only when the token set changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.markets: list = []
        self.generation = 0
        self.last_error = ""

    def replace(self, markets: list) -> None:
        key = market_key(markets)
        with self._lock:
            if key != market_key(self.markets):
                self.generation += 1
            self.markets = list(markets)

    def snapshot(self) -> tuple[list, int]:
        with self._lock:
            return list(self.markets), self.generation

    def note_error(self, message: str) -> None:
        with self._lock:
            self.last_error = str(message)[:240]


def token_map(markets: list) -> dict[str, TokenRef]:
    out: dict[str, TokenRef] = {}
    for market in markets:
        slug = str(market.slug)
        start = float(market.start_ts or 0.0)
        end = float(market.end_ts or 0.0)
        out[str(market.up_token)] = TokenRef(slug, "up", start, end)
        out[str(market.dn_token)] = TokenRef(slug, "down", start, end)
    return out


def bag_header(market: object, recv_ts: float) -> dict:
    return {
        "event_type": "bag",
        "recv_ts": float(recv_ts),
        "exch_ts": None,
        "slug": str(getattr(market, "slug", "")),
        "leg": "",
        "start_ts": float(getattr(market, "start_ts", 0.0) or 0.0),
        "end_ts": float(getattr(market, "end_ts", 0.0) or 0.0),
        "condition_id": str(getattr(market, "condition_id", "")),
        "up_token": str(getattr(market, "up_token", "")),
        "down_token": str(getattr(market, "dn_token", "")),
    }


def consume_market(
    ws: Any,
    *,
    tokens: dict[str, TokenRef],
    writer: TapeWriter,
    stop: threading.Event,
    generation: int,
    current_generation: Callable[[], int],
    silence_s: float = 25.0,
    ping_s: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
) -> str:
    """Read one socket until stop, a token-set change, or silence."""
    last_rx = clock()
    last_ping = last_rx
    while not stop.is_set():
        if current_generation() != generation:
            return "resubscribe"
        now_m = clock()
        if now_m - last_ping >= ping_s:
            ws.send("PING")
            last_ping = now_m
        if now_m - last_rx >= silence_s:
            return "silence"
        try:
            raw = ws.recv()
        except Exception as exc:
            if _is_timeout(exc):
                continue
            raise
        if raw is None:
            return "closed"
        if raw == "" or raw == b"":
            last_rx = clock()
            continue
        if _is_ping(raw):
            ws.send("PONG")
            last_rx = clock()
            continue
        if _is_pong(raw):
            last_rx = clock()
            continue
        last_rx = clock()
        recv_ts = float(wall())
        for row in records_from_payload(raw, recv_ts, tokens):
            writer.write(row)
    return "stop"


def subscribe_payload(asset_ids: list[str]) -> dict:
    return {
        "assets_ids": list(asset_ids),
        "type": "market",
        "custom_feature_enabled": True,
    }


def _close_ws(ws: Any) -> None:
    try:
        ws.close()
    except Exception:
        return


def book_loop(
    state: MarketState,
    writer: TapeWriter,
    stop: threading.Event,
    *,
    url: str = CLOB_MARKET_URL,
    connect: Optional[Callable[..., Any]] = None,
) -> None:
    import websocket

    opener = connect or (lambda target: websocket.create_connection(target, timeout=20))
    attempt = 0
    while not stop.is_set():
        markets, generation = state.snapshot()
        if not markets:
            stop.wait(2.0)
            continue
        tokens = token_map(markets)
        ids = list(tokens)
        ws = None
        try:
            ws = opener(url)
            ws.settimeout(1.0)
            ws.send(json.dumps(subscribe_payload(ids), separators=(",", ":")))
            now = time.time()
            for market in markets:
                writer.write(bag_header(market, now))
            attempt = 0
            why = consume_market(
                ws,
                tokens=tokens,
                writer=writer,
                stop=stop,
                generation=generation,
                current_generation=lambda: state.snapshot()[1],
            )
            if why == "silence":
                log_event("recorder_book_silence", tokens=len(ids))
        except Exception as exc:
            attempt += 1
            state.note_error(str(exc))
            log_event("recorder_book_error", error=str(exc)[:200], backoff_s=backoff_s(attempt))
            stop.wait(backoff_s(attempt))
        finally:
            if ws is not None:
                _close_ws(ws)
        if stop.is_set():
            return
        # A clean resubscribe should not pile a long sleep on the next window.
        if attempt == 0:
            stop.wait(0.2)


def oracle_loop(
    state: MarketState,
    writer: TapeWriter,
    stop: threading.Event,
    *,
    connect: Optional[Callable[..., Any]] = None,
) -> None:
    from buy.oracle_log import RTDS_URL, SUBSCRIBE_FRAME
    import websocket

    opener = connect or (lambda target: websocket.create_connection(target, timeout=20))
    attempt = 0
    while not stop.is_set():
        ws = None
        try:
            ws = opener(RTDS_URL)
            ws.settimeout(1.0)
            ws.send(json.dumps(SUBSCRIBE_FRAME, separators=(",", ":")))
            log_event("recorder_oracle_open")
            attempt = 0
            last_rx = time.monotonic()
            last_ping = last_rx
            while not stop.is_set():
                now_m = time.monotonic()
                if now_m - last_ping >= 5.0:
                    ws.send("PING")
                    last_ping = now_m
                if now_m - last_rx >= 45.0:
                    log_event("recorder_oracle_silence")
                    break
                try:
                    raw = ws.recv()
                except Exception as exc:
                    if _is_timeout(exc):
                        continue
                    raise
                if raw is None:
                    break
                if raw == "" or raw == b"":
                    last_rx = time.monotonic()
                    continue
                if _is_ping(raw):
                    ws.send("PONG")
                    last_rx = time.monotonic()
                    continue
                if _is_pong(raw):
                    last_rx = time.monotonic()
                    continue
                last_rx = time.monotonic()
                recv_ts = time.time()
                rows = oracle_records(raw, recv_ts)
                for row in rows:
                    writer.write(oracle_tape_row(row))
        except Exception as exc:
            attempt += 1
            state.note_error(str(exc))
            log_event("recorder_oracle_error", error=str(exc)[:200], backoff_s=backoff_s(attempt))
            stop.wait(backoff_s(attempt))
        finally:
            if ws is not None:
                _close_ws(ws)


def discover_loop(
    state: MarketState,
    stop: threading.Event,
    *,
    gamma_url: str = GAMMA_URL,
    interval_s: float = 20.0,
    grace_s: float = 30.0,
    discover: Optional[Callable[[], list]] = None,
) -> None:
    previous: list = []
    gateway = None
    while not stop.is_set():
        try:
            if discover is not None:
                found = discover()
            else:
                if gateway is None:
                    from buy.market import MarketGateway

                    gateway = MarketGateway(
                        gamma_url=gamma_url,
                        data_api_url=DATA_API_URL,
                        timeout=15.0,
                    )
                found = gateway.discover(SERIES)
            chosen = bags_to_record(found, previous, time.time(), grace_s=grace_s)
            state.replace(chosen)
            previous = chosen
            if not chosen:
                log_event("recorder_discover_empty")
        except Exception as exc:
            state.note_error(str(exc))
            log_event("recorder_discover_error", error=str(exc)[:200])
        stop.wait(interval_s)


def _repo_for(tape_dir: Path) -> Path:
    if tape_dir.name == "tape" and tape_dir.parent.name == "data":
        return tape_dir.parent.parent
    return tape_dir.parent


def _guard_disk(writer: TapeWriter, tape_dir: Path) -> dict:
    root = _repo_for(tape_dir)
    return enforce_tape_cap(tape_dir, root / "logs" / "durable", writer=writer)


def _note_disk(writer: TapeWriter, tape_dir: Path) -> dict:
    try:
        disk = _guard_disk(writer, tape_dir)
    except OSError as exc:
        log_event("recorder_disk_error", error=str(exc)[:200])
        return {"deleted": [], "free_bytes": None, "tape_bytes": None, "writes_enabled": writer.writes_enabled}
    if disk.get("deleted") or not disk.get("writes_enabled", True):
        log_event(
            "recorder_disk",
            deleted=disk.get("deleted"),
            free_bytes=disk.get("free_bytes"),
            tape_bytes=disk.get("tape_bytes"),
            writes_enabled=disk.get("writes_enabled"),
        )
    return disk


def run(
    *,
    tape_dir: Path | str,
    stop_file: Path | str,
    seconds: float = 0.0,
    gamma_url: str = GAMMA_URL,
) -> int:
    """Block until SIGTERM, ``stop_file``, or ``seconds`` (0 = forever)."""
    stop = threading.Event()
    writer = TapeWriter(tape_dir)
    state = MarketState()

    def _stop_signal(_signum: int, _frame: object) -> None:
        stop.set()

    import signal

    signal.signal(signal.SIGTERM, _stop_signal)
    signal.signal(signal.SIGINT, _stop_signal)
    spool = Path(tape_dir)
    disk = _note_disk(writer, spool)
    log_event(
        "recorder_start",
        tape_dir=str(tape_dir),
        rss=rss_bytes(),
        writes=disk.get("writes_enabled"),
        tape_bytes=disk.get("tape_bytes"),
        free_bytes=disk.get("free_bytes"),
    )
    threads = [
        threading.Thread(target=discover_loop, args=(state, stop), kwargs={"gamma_url": gamma_url}, name="recorder-discover", daemon=True),
        threading.Thread(target=book_loop, args=(state, writer, stop), name="recorder-book", daemon=True),
        threading.Thread(target=oracle_loop, args=(state, writer, stop), name="recorder-oracle", daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + float(seconds) if seconds and seconds > 0 else None
    stop_path = Path(stop_file)
    last_status = time.monotonic()
    try:
        while not stop.is_set():
            if stop_path.exists():
                log_event("recorder_stop_file")
                stop.set()
                break
            now_m = time.monotonic()
            if deadline is not None and now_m >= deadline:
                log_event("recorder_seconds_done", seconds=seconds)
                stop.set()
                break
            try:
                writer.flush()
                writer.release_stale(time.time())
            except OSError as exc:
                writer.set_writes_enabled(False)
                log_event("recorder_flush_error", error=str(exc)[:200])
            if now_m - last_status >= 60.0:
                disk = _note_disk(writer, spool)
                markets, generation = state.snapshot()
                log_event(
                    "recorder_status",
                    markets=[str(getattr(m, "slug", "")) for m in markets],
                    generation=generation,
                    rows=writer.rows,
                    dropped=writer.dropped,
                    open_files=writer.open_count(),
                    writes=writer.writes_enabled,
                    rss=rss_bytes(),
                    last_error=state.last_error,
                    tape_bytes=disk.get("tape_bytes"),
                    free_bytes=disk.get("free_bytes"),
                )
                last_status = now_m
            stop.wait(1.0)
    finally:
        stop.set()
        writer.flush(force=True)
        writer.close()
        log_event("recorder_exit", rows=writer.rows, rss=rss_bytes())
    return 0
