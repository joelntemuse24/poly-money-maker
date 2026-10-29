"""Pre-signed order support for Polymarket CLOB orders.

This module provides a small, dependency-light layer that:

* pre-signs market orders once and reuses the exact signed payload bytes,
* pre-computes L2 (HMAC) authentication headers for the current and next integer
  seconds so hot-path order submission performs zero HMAC/base64 work,
* posts the pre-serialized body directly to the CLOB ``/order`` endpoint,
* offers transport/latency helpers (uvloop, TCP_NODELAY, fastest Cloudflare edge probing).

The cache is single-use: an entry is popped when consumed so that the order
salt is never reused, which would otherwise be rejected by the exchange.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:  # orjson is optional; it is substantially faster when present.
    import orjson  # type: ignore

    _HAS_ORJSON = True
except Exception:  # pragma: no cover - optional dependency
    orjson = None  # type: ignore
    _HAS_ORJSON = False


@dataclass
class PreSignedEntry:
    """A signed order together with everything needed to submit it fast."""

    token_id: str
    side: str
    amount: float
    price: float
    order_type: str
    signed_order: Any
    order_payload: dict
    serialized_body: str
    serialized_bytes: bytes
    created_at: float
    client_id: int
    signer_address: str = ""
    creds: Any = None
    l2_headers_by_sec: dict[int, dict[str, str]] = field(default_factory=dict)

    def warm_l2_headers(self, now_sec: Optional[int] = None) -> None:
        """Pre-compute L2 HMAC headers for ``now_sec``, ``now_sec+1``, and ``now_sec+2``."""
        if not self.signer_address or self.creds is None or not self.serialized_body:
            return
        base_sec = int(time.time()) if now_sec is None else int(now_sec)
        for sec in (base_sec, base_sec + 1, base_sec + 2):
            if sec not in self.l2_headers_by_sec:
                try:
                    self.l2_headers_by_sec[sec] = fast_l2_headers(
                        self.signer_address,
                        self.creds,
                        "POST",
                        "/order",
                        self.serialized_body,
                        timestamp=sec,
                    )
                except Exception:
                    return
        if len(self.l2_headers_by_sec) > 6:
            for old_sec in [k for k in self.l2_headers_by_sec if k < base_sec - 1]:
                self.l2_headers_by_sec.pop(old_sec, None)

    def get_l2_headers(self, signer_address: str, creds: Any, now_sec: Optional[int] = None) -> dict[str, str]:
        """Return pre-computed L2 headers for ``now_sec`` on hit, else compute immediately."""
        sec = int(time.time()) if now_sec is None else int(now_sec)
        cached = self.l2_headers_by_sec.get(sec)
        if (
            cached is not None
            and cached.get("POLY_ADDRESS") == signer_address
            and cached.get("POLY_API_KEY") == getattr(creds, "api_key", None)
        ):
            return cached
        headers = fast_l2_headers(
            signer_address,
            creds,
            "POST",
            "/order",
            self.serialized_body,
            timestamp=sec,
        )
        self.l2_headers_by_sec[sec] = headers
        return headers


# Cache decoded API secrets so the base64 work happens only once per secret.
_SECRET_CACHE: dict[str, bytes] = {}


def fast_serialize_json(payload: dict) -> str:
    """Serialize ``payload`` to a compact JSON string as quickly as possible."""
    if _HAS_ORJSON:
        return orjson.dumps(payload).decode("utf-8")
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _decode_secret(api_secret: str) -> bytes:
    """Return the decoded secret bytes, memoized by the raw secret string."""
    cached = _SECRET_CACHE.get(api_secret)
    if cached is not None:
        return cached
    decoded = base64.urlsafe_b64decode(api_secret)
    _SECRET_CACHE[api_secret] = decoded
    return decoded


def fast_l2_headers(
    signer_address: str,
    creds: Any,
    method: str,
    request_path: str,
    serialized_body: str,
    timestamp: Optional[int] = None,
) -> dict:
    """Build Polymarket L2 (HMAC-SHA256) auth headers for a request."""
    ts_str = str(int(time.time()) if timestamp is None else int(timestamp))
    secret_bytes = _decode_secret(creds.api_secret)
    message = (ts_str + method + request_path + serialized_body).encode("utf-8")
    sig = base64.urlsafe_b64encode(
        hmac.new(secret_bytes, message, hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "POLY_ADDRESS": signer_address,
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": ts_str,
        "POLY_API_KEY": creds.api_key,
        "POLY_PASSPHRASE": creds.api_passphrase,
        "User-Agent": "py_clob_client_v2",
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Content-Type": "application/json",
    }


def _order_to_json(signed_order: Any, owner: str, order_type: str, post_only: bool, defer_exec: bool):
    """Try the py_clob_client_v2 helpers; return ``None`` when unavailable."""
    try:
        from py_clob_client_v2.client import order_to_json_v2  # type: ignore
    except Exception:
        order_to_json_v2 = None  # type: ignore

    try:
        from py_clob_client_v2.client import order_to_json_v1  # type: ignore
    except Exception:
        order_to_json_v1 = None  # type: ignore

    # v2 signature first; v1 is the fallback for older clients.
    for helper in (order_to_json_v2, order_to_json_v1):
        if helper is None:
            continue
        try:
            return helper(signed_order, str(owner or ""), order_type, post_only, defer_exec)
        except TypeError:
            # Some versions expose a different positional/kw signature.
            try:
                return helper(
                    signed_order,
                    order_type=order_type,
                    post_only=post_only,
                    defer_exec=defer_exec,
                )
            except Exception:
                continue
        except Exception:
            continue
    return None


def build_order_payload(
    signed_order: Any,
    owner: str,
    order_type: str = "FAK",
    post_only: bool = False,
    defer_exec: bool = True,
) -> dict:
    """Build the JSON body posted to ``/order`` for ``signed_order``."""
    if hasattr(signed_order, "salt") and hasattr(signed_order, "signature"):
        payload = _order_to_json(signed_order, owner, order_type, post_only, defer_exec)
        if isinstance(payload, dict):
            return payload

    return {
        "order": signed_order if isinstance(signed_order, dict) else {"signed": str(signed_order)},
        "owner": str(owner or ""),
        "orderType": str(order_type),
        "deferExec": bool(defer_exec),
        "postOnly": bool(post_only),
    }


class PreSignedOrderCache:
    """Thread-safe, TTL-bounded cache of pre-signed market orders."""

    def __init__(self, ttl_s: float = 360.0, max_entries: int = 256) -> None:
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._entries: "collections.OrderedDict[tuple, PreSignedEntry]" = (
            collections.OrderedDict()
        )
        self._warmer_started = False

    @staticmethod
    def _key(client: Any, token_id: Any, side: Any, amount: Any, price: Any, order_type: Any) -> tuple:
        return (
            id(client),
            str(token_id),
            str(side).upper(),
            round(float(amount), 4),
            round(float(price), 4),
            str(order_type).upper(),
        )

    def _expired(self, entry: PreSignedEntry) -> bool:
        return (time.monotonic() - entry.created_at) > self.ttl_s

    def refresh_all_l2_headers(self, now_sec: Optional[int] = None) -> int:
        """Pre-compute L2 headers for all live cached entries for the current & next second."""
        with self._lock:
            entries = [e for e in self._entries.values() if not self._expired(e)]
        sec = int(time.time()) if now_sec is None else int(now_sec)
        warmed = 0
        for entry in entries:
            entry.warm_l2_headers(sec)
            if sec in entry.l2_headers_by_sec:
                warmed += 1
        return warmed

    def _ensure_header_warmer(self) -> None:
        if self._warmer_started:
            return
        self._warmer_started = True

        def _loop() -> None:
            while True:
                try:
                    self.refresh_all_l2_headers()
                except Exception:
                    pass
                time.sleep(0.4)

        threading.Thread(target=_loop, name="presign-l2-warmer", daemon=True).start()

    def put(
        self,
        client: Any,
        token_id: str,
        side: str,
        amount: float,
        price: float,
        signed_order: Any,
        order_type: str = "FAK",
        owner: str = "",
        defer_exec: bool = True,
    ) -> PreSignedEntry:
        """Build and store a pre-signed entry, returning it."""
        payload = build_order_payload(
            signed_order,
            owner,
            order_type=order_type,
            defer_exec=defer_exec,
        )
        body = fast_serialize_json(payload)
        signer_addr = ""
        creds = getattr(client, "creds", None)
        signer = getattr(client, "signer", None)
        if signer is not None and callable(getattr(signer, "address", None)):
            try:
                signer_addr = str(signer.address() or "")
            except Exception:
                signer_addr = ""
        entry = PreSignedEntry(
            token_id=str(token_id),
            side=str(side).upper(),
            amount=float(amount),
            price=float(price),
            order_type=str(order_type).upper(),
            signed_order=signed_order,
            order_payload=payload,
            serialized_body=body,
            serialized_bytes=body.encode("utf-8"),
            created_at=time.monotonic(),
            client_id=id(client),
            signer_address=signer_addr,
            creds=creds,
        )
        entry.warm_l2_headers()
        if signer_addr and creds is not None:
            self._ensure_header_warmer()
        key = self._key(client, token_id, side, amount, price, order_type)
        with self._lock:
            # Drop expired entries opportunistically.
            if self._entries:
                stale = [k for k, v in self._entries.items() if self._expired(v)]
                for k in stale:
                    self._entries.pop(k, None)
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return entry

    def pop(
        self,
        client: Any,
        token_id: str,
        side: str,
        amount: float,
        price: float,
        order_type: str = "FAK",
    ) -> Optional[PreSignedEntry]:
        """Remove and return a matching entry; entries are single-use."""
        key = self._key(client, token_id, side, amount, price, order_type)
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None and self._expired(entry):
                return None
            return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def presign_market_order(
        self,
        client: Any,
        token_id: str,
        side: str,
        amount: float,
        price: float,
        tick_size: str = "0.01",
        neg_risk: bool = False,
        version: int = 2,
        order_type: str = "FAK",
    ) -> Optional[PreSignedEntry]:
        """Sign a market order with ``client`` and cache the result."""
        from py_clob_client_v2.clob_types import MarketOrderArgs, OrderArgs  # type: ignore
        from py_clob_client_v2.order_builder.constants import BUY, SELL  # type: ignore

        try:
            from py_clob_client_v2.clob_types import CreateOrderOptions  # type: ignore
        except Exception:  # pragma: no cover - older layouts
            from py_clob_client_v2.client import CreateOrderOptions  # type: ignore

        side_val = str(side).upper()
        if side_val in ("BUY", "BID"):
            side_const = BUY
        elif side_val in ("SELL", "ASK"):
            side_const = SELL
        else:
            side_const = side

        order_args = MarketOrderArgs(
            token_id=str(token_id),
            amount=float(amount),
            side=side_const,
            price=float(price),
            order_type=str(order_type),
        )

        builder_config = getattr(client, "builder_config", None)
        builder_code = getattr(builder_config, "builder_code", None)
        if builder_code is not None:
            try:
                order_args.builder_code = builder_code
            except Exception:
                pass

        options = CreateOrderOptions(
            tick_size=str(tick_size),
            neg_risk=bool(neg_risk),
        )
        signed_order = client.builder.build_market_order(
            order_args,
            options,
            version=int(version),
        )
        owner = ""
        try:
            creds = getattr(client, "creds", None)
            signer = getattr(client, "signer", None)
            if creds is not None and getattr(creds, "api_key", None):
                owner = str(creds.api_key)
            elif signer is not None and hasattr(signer, "address"):
                owner = signer.address()
        except Exception:
            owner = ""
        return self.put(
            client,
            token_id,
            side,
            amount,
            price,
            signed_order,
            order_type=order_type,
            owner=owner,
        )


_ORDER_HTTP_CLIENT: Any = None
_LAST_ORDER_ACTIVITY_TS: float = 0.0
# Number of order POSTs currently on the wire. Plain int assignments only, so
# neither the POST path nor the warmer ever waits on a lock.
_ORDER_INFLIGHT: int = 0

_ORDER_CLIENT_FACTORY: Any = None
_ORDER_WARM_THREAD: Any = None
_ORDER_WARM_STOP: Optional[threading.Event] = None
_ORDER_WARM_STATS: dict[str, Any] = {
    "pings_ok": 0,
    "pings_fail": 0,
    "reconnects": 0,
    "reconnect_fail": 0,
    "skips_inflight": 0,
    "skips_recent": 0,
    "last_rtt_ms": None,
}

ORDER_WARM_TIMEOUT_S = 1.0
ORDER_WARM_SUMMARY_S = 60.0
ORDER_CLIENT_CLOSE_DELAY_S = 10.0


def set_order_http_client(http_client: Any, factory: Any = None) -> None:
    """Register a dedicated warm HTTP/2 client for ``fast_post_order``."""
    global _ORDER_HTTP_CLIENT, _ORDER_CLIENT_FACTORY
    _ORDER_HTTP_CLIENT = http_client
    if factory is not None:
        _ORDER_CLIENT_FACTORY = factory


def get_order_http_client() -> Any:
    return _ORDER_HTTP_CLIENT


def last_order_activity_ts() -> float:
    return _LAST_ORDER_ACTIVITY_TS


def order_inflight() -> int:
    """Return the number of order POSTs currently in flight."""
    return _ORDER_INFLIGHT


def order_conn_stats() -> dict:
    """Return a snapshot of the order connection warmer counters."""
    return dict(_ORDER_WARM_STATS)


def default_order_client_factory() -> Any:
    """Build a fresh HTTP/2 client with the same limits the order path uses."""
    import httpx

    return httpx.Client(http2=True, limits=httpx.Limits(keepalive_expiry=120))


def _close_order_client_later(old_client: Any, delay_s: float) -> None:
    """Close ``old_client`` in the background so in-flight POSTs on it finish."""

    def _close() -> None:
        if delay_s > 0:
            time.sleep(delay_s)
        try:
            old_client.close()
        except Exception:
            pass

    try:
        threading.Thread(target=_close, name="order-conn-close", daemon=True).start()
    except Exception:
        pass


def reconnect_order_http_client(
    factory: Any = None, close_delay_s: float = ORDER_CLIENT_CLOSE_DELAY_S
) -> Any:
    """Swap in a fresh order client and close the old one after a delay."""
    global _ORDER_HTTP_CLIENT
    make = factory or _ORDER_CLIENT_FACTORY or default_order_client_factory
    try:
        new_client = make()
    except Exception:
        _ORDER_WARM_STATS["reconnect_fail"] += 1
        return None
    old_client = _ORDER_HTTP_CLIENT
    _ORDER_HTTP_CLIENT = new_client
    _ORDER_WARM_STATS["reconnects"] += 1
    if old_client is not None and old_client is not new_client:
        _close_order_client_later(old_client, float(close_delay_s))
    return new_client


def _order_ping_ok(resp: Any) -> bool:
    status = getattr(resp, "status_code", None)
    if status is None:
        return True
    try:
        return int(status) == 200
    except (TypeError, ValueError):
        return False


def order_warm_tick(
    host: str,
    interval_s: float = 2.0,
    *,
    factory: Any = None,
    timeout: float = ORDER_WARM_TIMEOUT_S,
    close_delay_s: float = ORDER_CLIENT_CLOSE_DELAY_S,
) -> str:
    """Run one warmer pass; return ``ok``, ``fail``, ``inflight``, ``recent`` or ``no_client``."""
    now = time.time()
    if _ORDER_INFLIGHT > 0:
        _ORDER_WARM_STATS["skips_inflight"] += 1
        return "inflight"
    if _LAST_ORDER_ACTIVITY_TS and (now - _LAST_ORDER_ACTIVITY_TS) < interval_s:
        _ORDER_WARM_STATS["skips_recent"] += 1
        return "recent"
    hclient = _ORDER_HTTP_CLIENT
    get = getattr(hclient, "get", None)
    if not callable(get):
        return "no_client"
    start = time.perf_counter()
    try:
        resp = get(f"{str(host).rstrip('/')}/time", timeout=timeout)
        ok = _order_ping_ok(resp)
    except Exception:
        ok = False
    rtt_ms = (time.perf_counter() - start) * 1000.0
    if ok:
        _ORDER_WARM_STATS["pings_ok"] += 1
        _ORDER_WARM_STATS["last_rtt_ms"] = round(rtt_ms, 2)
        return "ok"
    _ORDER_WARM_STATS["pings_fail"] += 1
    reconnect_order_http_client(factory, close_delay_s=close_delay_s)
    return "fail"


def start_order_conn_warmer(
    host: str,
    interval_s: float = 2.0,
    *,
    factory: Any = None,
    log: Any = None,
    timeout: float = ORDER_WARM_TIMEOUT_S,
    summary_s: float = ORDER_WARM_SUMMARY_S,
    close_delay_s: float = ORDER_CLIENT_CLOSE_DELAY_S,
) -> Any:
    """Keep the dedicated order HTTP/2 connection warm with a ``/time`` GET every ``interval_s``.

    Pings skip while an order is in flight or one was sent within the last
    interval, so they never contend with a POST. A ping error (or non-200)
    builds a fresh client through ``factory``, swaps it in, and closes the old
    one in the background. ``log`` (called as ``log(event, **fields)``) gets a
    compact ``order_conn_warm`` summary at most every ``summary_s``.
    """
    global _ORDER_WARM_THREAD, _ORDER_WARM_STOP, _ORDER_CLIENT_FACTORY
    thread = _ORDER_WARM_THREAD
    if thread is not None and thread.is_alive():
        return thread
    base = str(host or "").rstrip("/")
    if not base:
        return None
    if callable(factory):
        _ORDER_CLIENT_FACTORY = factory
    interval = max(0.01, float(interval_s or 2.0))
    stop = threading.Event()
    _ORDER_WARM_STOP = stop

    def _run() -> None:
        last_log = time.monotonic()
        while not stop.is_set():
            try:
                order_warm_tick(
                    base,
                    interval,
                    factory=factory,
                    timeout=timeout,
                    close_delay_s=close_delay_s,
                )
            except Exception:
                pass
            if callable(log) and (time.monotonic() - last_log) >= summary_s:
                last_log = time.monotonic()
                try:
                    log(
                        "order_conn_warm",
                        interval_s=interval,
                        **order_conn_stats(),
                    )
                except Exception:
                    pass
            stop.wait(interval)

    thread = threading.Thread(target=_run, name="order-conn-warmer", daemon=True)
    _ORDER_WARM_THREAD = thread
    thread.start()
    return thread


def stop_order_conn_warmer() -> None:
    """Signal the order connection warmer to exit."""
    global _ORDER_WARM_THREAD, _ORDER_WARM_STOP
    stop = _ORDER_WARM_STOP
    if stop is not None:
        stop.set()
    _ORDER_WARM_STOP = None
    _ORDER_WARM_THREAD = None


def fast_post_order(
    client: Any,
    signed_order: Any,
    entry: Optional[PreSignedEntry] = None,
    order_type: Any = "FAK",
    defer_exec: bool = True,
    *,
    http_client: Any = None,
    post_meta: Optional[dict] = None,
):
    """Post an order, bypassing client re-serialization and using pre-computed L2 headers when possible.

    ``post_meta`` (optional) is filled in place with send/response epochs and the raw
    response, so the caller can build a latency log after the POST returns.
    """
    global _LAST_ORDER_ACTIVITY_TS, _ORDER_INFLIGHT
    if (
        entry is not None
        and entry.serialized_body
        and type(client).__name__ == "ClobClient"
        and getattr(client, "creds", None) is not None
        and getattr(client, "signer", None) is not None
    ):
        headers = entry.get_l2_headers(client.signer.address(), client.creds)
        url = f"{str(client.host).rstrip('/')}/order"
        hclient = http_client if http_client is not None else _ORDER_HTTP_CLIENT
        if hclient is not None and callable(getattr(hclient, "post", None)):
            _LAST_ORDER_ACTIVITY_TS = time.time()
            _ORDER_INFLIGHT += 1
            try:
                if post_meta is None:
                    resp = hclient.post(url, headers=headers, content=entry.serialized_bytes)
                else:
                    resp = _post_capture_timing(
                        hclient, url, headers, entry.serialized_bytes, post_meta
                    )
            finally:
                _ORDER_INFLIGHT -= 1
            if post_meta is not None:
                post_meta.setdefault("resp_complete_ts", time.time())
                record_response_meta(post_meta, resp)
            if getattr(resp, "status_code", 200) != 200:
                try:
                    from py_clob_client_v2.exceptions import PolyApiException  # type: ignore

                    raise PolyApiException(resp)
                except ImportError:
                    raise RuntimeError(f"CLOB order post failed status={resp.status_code}")
            if _HAS_ORJSON and hasattr(resp, "content"):
                body = orjson.loads(resp.content)
            else:
                body = resp.json()
            if post_meta is not None:
                post_meta["server_body_ts"] = response_body_ts(body)
            return body
        if callable(getattr(client, "_post", None)):
            _LAST_ORDER_ACTIVITY_TS = time.time()
            if post_meta is not None:
                post_meta["send_start_ts"] = time.time()
            result = client._post(
                url,
                headers=headers,
                data=entry.serialized_body,
            )
            if post_meta is not None:
                post_meta["resp_complete_ts"] = time.time()
                post_meta["server_body_ts"] = response_body_ts(result)
            return result
    if post_meta is not None:
        post_meta["send_start_ts"] = time.time()
    result = client.post_order(signed_order, order_type=order_type)
    if post_meta is not None:
        post_meta["resp_complete_ts"] = time.time()
        post_meta["server_body_ts"] = response_body_ts(result)
    return result


_BODY_TS_KEYS = ("timestamp", "serverTime", "server_time", "transactTime", "matchTime", "ts")

# Response headers that describe server-side time; journaled verbatim when present.
_TIMING_HEADERS = (("server-timing", "server_timing"), ("x-response-time", "x_response_time"),
                   ("cf-ray", "cf_ray"), ("x-request-id", "x_request_id"),
                   ("cf-cache-status", "cf_cache_status"))

# Identity of the connection the previous order POST used (diagnostics only).
_LAST_CONN: dict[str, Any] = {"ident": None}


def _post_capture_timing(hclient: Any, url: str, headers: dict, body: bytes,
                         post_meta: dict) -> Any:
    """POST with the response-headers timestamp captured as ``resp_first_byte_ts``.

    An httpx client is driven streamed: ``send(..., stream=True)`` returns as soon
    as the status line and headers are decoded, so the stamp measures true
    time-to-first-byte instead of the request's write time. The body is read
    immediately afterwards. Any other client keeps the plain ``post`` call.
    """
    build, send = getattr(hclient, "build_request", None), getattr(hclient, "send", None)
    if callable(build) and callable(send):
        request = build("POST", url, headers=headers, content=body)
        post_meta["send_start_ts"] = time.time()
        resp = send(request, stream=True)
        post_meta["resp_first_byte_ts"] = time.time()
        try:
            resp.read()
        finally:
            post_meta["resp_complete_ts"] = time.time()
        return resp
    post_meta["send_start_ts"] = time.time()
    resp = hclient.post(url, headers=headers, content=body)
    post_meta["resp_complete_ts"] = time.time()
    return resp


def connection_identity(resp: Any) -> Optional[str]:
    """Stable per-connection token for ``resp`` (local address, else stream id)."""
    extensions = getattr(resp, "extensions", None)
    if not isinstance(extensions, dict):
        return None
    stream = extensions.get("network_stream")
    extra = getattr(stream, "get_extra_info", None)
    if not callable(extra):
        return None if stream is None else str(id(stream))
    for info in ("client_addr", "socket"):
        try:
            value = extra(info)
        except Exception:
            value = None
        if value is None:
            continue
        if info == "socket":
            try:
                value = value.getsockname()
            except Exception:
                value = id(value)
        return f"{value}"
    return str(id(stream))


def record_response_meta(post_meta: dict, resp: Any) -> None:
    """Record HTTP status, timing headers and first response byte time from a response.

    ``resp_first_byte_ts`` is kept when the streaming POST already stamped it;
    the ``elapsed``-derived fallback only applies to a plain ``post`` client,
    where it approximates the send time rather than the header arrival.
    """
    try:
        post_meta["http_status"] = getattr(resp, "status_code", None)
    except Exception:
        post_meta["http_status"] = None
    headers = getattr(resp, "headers", None)
    get = getattr(headers, "get", None)
    try:
        post_meta["server_date"] = get("date") if callable(get) else None
    except Exception:
        post_meta["server_date"] = None
    for name, key in _TIMING_HEADERS:
        try:
            post_meta[key] = get(name) if callable(get) else None
        except Exception:
            post_meta[key] = None
    version = getattr(resp, "http_version", None)
    if version is None:
        extensions = getattr(resp, "extensions", None)
        if isinstance(extensions, dict):
            version = extensions.get("http_version")
    post_meta["http_version"] = (version.decode() if isinstance(version, bytes)
                                 else None if version is None else str(version))
    ident = connection_identity(resp)
    if ident is not None:
        post_meta["conn_reused"] = _LAST_CONN.get("ident") == ident
        _LAST_CONN["ident"] = ident
    else:
        post_meta["conn_reused"] = None
    if post_meta.get("resp_first_byte_ts") is not None:
        return
    first_byte = None
    elapsed = getattr(resp, "elapsed", None)
    complete = post_meta.get("resp_complete_ts")
    try:
        if elapsed is not None and complete is not None:
            first_byte = complete - float(elapsed.total_seconds())
    except Exception:
        first_byte = None
    post_meta["resp_first_byte_ts"] = first_byte


def response_body_ts(body: Any) -> Optional[float]:
    """Return the first server-side timestamp found in an order response body."""
    if not isinstance(body, dict):
        return None
    for key in _BODY_TS_KEYS:
        value = body.get(key)
        if value is not None:
            return value
    return None


def enable_uvloop_if_available() -> bool:
    """Install uvloop as the default event loop policy when installed."""
    try:
        import uvloop  # type: ignore

        uvloop.install()
        return True
    except Exception:
        return False


def configure_socket_nodelay(sock: Any) -> bool:
    """Enable TCP_NODELAY and SO_KEEPALIVE on ``sock``; return success."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return True
    except (OSError, AttributeError):
        return False


def configure_low_latency_sockets(sock: Any = None) -> Any:
    """Configure low-latency socket options or install uvloop when no socket."""
    if sock is not None:
        return configure_socket_nodelay(sock)
    uv = enable_uvloop_if_available()
    return {"uvloop": uv, "tcp_nodelay": True, "so_keepalive": True}


def select_fastest_cf_edge(
    host: str = "clob.polymarket.com",
    candidates: Optional[list[str]] = None,
    port: int = 443,
    timeout: float = 1.0,
) -> dict:
    """Probe TCP connect latency and return the fastest edge."""
    if candidates is None:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            infos = []
        seen: list[str] = []
        for info in infos:
            ip = info[4][0]
            if ip not in seen:
                seen.append(ip)
        candidates = seen

    results: list[dict] = []
    best_ip: Optional[str] = None
    best_ms: Optional[float] = None

    for ip in candidates:
        start = time.perf_counter()
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(float(timeout))
            configure_socket_nodelay(sock)
            sock.connect((ip, int(port)))
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            results.append({"ip": ip, "latency_ms": elapsed_ms, "ok": True})
            if best_ms is None or elapsed_ms < best_ms:
                best_ms = elapsed_ms
                best_ip = ip
        except OSError as exc:
            results.append({"ip": ip, "latency_ms": None, "ok": False, "error": str(exc)})
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    return {
        "host": host,
        "fastest_ip": best_ip,
        "latency_ms": best_ms,
        "results": results,
    }


DEFAULT_PRESIGN_CACHE = PreSignedOrderCache()


def get_presign_cache() -> PreSignedOrderCache:
    """Return the process-wide default PreSignedOrderCache singleton."""
    return DEFAULT_PRESIGN_CACHE
