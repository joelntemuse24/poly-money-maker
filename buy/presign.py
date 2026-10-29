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


def set_order_http_client(http_client: Any) -> None:
    """Register a dedicated warm HTTP/2 client for ``fast_post_order``."""
    global _ORDER_HTTP_CLIENT
    _ORDER_HTTP_CLIENT = http_client


def get_order_http_client() -> Any:
    return _ORDER_HTTP_CLIENT


def last_order_activity_ts() -> float:
    return _LAST_ORDER_ACTIVITY_TS


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
    global _LAST_ORDER_ACTIVITY_TS
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
            if post_meta is not None:
                post_meta["send_start_ts"] = time.time()
            resp = hclient.post(url, headers=headers, content=entry.serialized_bytes)
            if post_meta is not None:
                post_meta["resp_complete_ts"] = time.time()
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


def record_response_meta(post_meta: dict, resp: Any) -> None:
    """Record HTTP status, ``date`` header and first response byte time from an httpx response."""
    try:
        post_meta["http_status"] = getattr(resp, "status_code", None)
    except Exception:
        post_meta["http_status"] = None
    headers = getattr(resp, "headers", None)
    try:
        post_meta["server_date"] = headers.get("date") if headers is not None else None
    except Exception:
        post_meta["server_date"] = None
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
