"""Log-only tape of three BTC wallets on the Polymarket activity feed.

Subscribes to ``activity/trades`` and keeps fills by NIULAI4, asdaefef, and
dvasdkasodk in the BTC 5m and 15m markets. Comparison against our own
signals is arithmetic on the log. Nothing here places or changes an order.
"""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
from collections import deque
from typing import Any, Callable, Optional


ACTIVITY_URL = "wss://ws-live-data.polymarket.com"

SUBSCRIBE_FRAME = {
    "action": "subscribe",
    "subscriptions": [{"topic": "activity", "type": "trades"}],
}

# Address -> study name. Matching is case-insensitive.
WALLETS = {
    "0x44832d0d2ec11187c1e77d786feb15f6a50254c6": "NIULAI4",
    "0x75cc3b63a2f2423085e10706c78b494017b93ce1": "asdaefef",
    "0x5d4aba8ad45bb5eab3499a0294b42da5d1e455d3": "dvasdkasodk",
}

BTC_SLUG_PREFIXES = ("btc-updown-5m-", "btc-updown-15m-")


def is_btc_updown(slug: str) -> bool:
    text = str(slug or "").strip().lower()
    return any(text.startswith(prefix) for prefix in BTC_SLUG_PREFIXES)


def window_kind(slug: str) -> str:
    text = str(slug or "").lower()
    if "updown-5m-" in text:
        return "5m"
    if "updown-15m-" in text:
        return "15m"
    return ""


def _num(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _seconds(value: Any) -> Optional[float]:
    parsed = _num(value)
    if parsed is None:
        return None
    if parsed > 10_000_000_000:
        parsed = parsed / 1000.0
    return parsed


def _as_payload(raw: Any) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.upper() in {"PING", "PONG"}:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return raw


def _btc_slug(payload: dict) -> Optional[str]:
    for key in ("slug", "eventSlug"):
        text = str(payload.get(key) or "").strip()
        if is_btc_updown(text):
            return text
    return None


def parse_activity_frame(raw: Any) -> Optional[list[dict]]:
    """Payload dicts from one ``activity/trades`` frame.

    ``None`` when the frame is not that topic. An empty list means the
    frame was a trade message with nothing we can read.
    """
    parsed = _as_payload(raw)
    if parsed is None:
        return None
    frames = parsed if isinstance(parsed, list) else [parsed]
    found = False
    payloads: list[dict] = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        topic = str(frame.get("topic") or "")
        kind = str(frame.get("type") or "")
        body = frame.get("payload")
        if topic != "activity" or kind != "trades" or not isinstance(body, dict):
            continue
        found = True
        item = dict(body)
        item["_outer_ts"] = frame.get("timestamp")
        payloads.append(item)
    if not found:
        return None
    return payloads


def normalize_wallet_fill(payload: dict, *, recv_ts: float) -> Optional[dict]:
    """One watched BTC 5m/15m fill, or None.

    ``their_ts`` is the payload timestamp (unix seconds). The match itself
    can print 0–3s before that block time, so ``recv_ts`` is kept as well.
    """
    if not isinstance(payload, dict):
        return None
    wallet = str(payload.get("proxyWallet") or "").strip().lower()
    name = WALLETS.get(wallet)
    if not name:
        return None
    slug = _btc_slug(payload)
    if not slug:
        return None
    price = _num(payload.get("price"))
    size = _num(payload.get("size"))
    their_ts = _seconds(payload.get("timestamp"))
    if price is None or size is None or their_ts is None:
        return None
    if price <= 0 or size <= 0:
        return None
    outcome = str(payload.get("outcome") or "")
    trade_side = str(payload.get("side") or "").upper()
    return {
        "wallet": wallet,
        "name": name,
        "slug": slug,
        "duration": window_kind(slug),
        "condition_id": str(payload.get("conditionId") or ""),
        "asset": str(payload.get("asset") or ""),
        "outcome": outcome,
        "trade_side": trade_side,
        "price": price,
        "size": size,
        "their_ts": their_ts,
        "outer_ts": _seconds(payload.get("_outer_ts")),
        "recv_ts": float(recv_ts),
        "tx": str(payload.get("transactionHash") or ""),
    }


def fill_key(fill: dict) -> str:
    tx = str(fill.get("tx") or "")
    wallet = str(fill.get("wallet") or "")
    if tx:
        return "|".join(
            (
                tx,
                wallet,
                str(fill.get("asset") or ""),
                str(fill.get("trade_side") or ""),
                str(fill.get("price")),
                str(fill.get("size")),
            )
        )
    return "|".join(
        (
            wallet,
            str(fill.get("slug") or ""),
            str(fill.get("outcome") or ""),
            str(fill.get("trade_side") or ""),
            str(fill.get("price")),
            str(fill.get("size")),
            str(fill.get("their_ts")),
        )
    )


def _percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * float(p)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _our_price(attempt: dict) -> tuple[Optional[float], str]:
    shares = _num(attempt.get("shares"))
    vwap = _num(attempt.get("vwap"))
    if shares is not None and shares > 0 and vwap is not None and vwap > 0:
        return vwap, "vwap"
    ask = _num(attempt.get("ask"))
    if ask is not None and ask > 0:
        return ask, "ask"
    limit = _num(attempt.get("limit"))
    if limit is not None and limit > 0:
        return limit, "limit"
    return None, ""


def attempts_from_rows(rows: list[dict]) -> list[dict]:
    """One attempt per signal. A later paper fill or entry adds post/ack."""
    by_key: dict[tuple, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        if event not in {"signal", "paper_fill", "entry"}:
            continue
        slug = str(row.get("slug") or "")
        decision_ts = _num(row.get("decision_ts"))
        if not slug or decision_ts is None:
            continue
        strategy = str(row.get("strategy") or "")
        side = str(row.get("side") or "").lower()
        key = (slug, strategy, side, round(decision_ts, 3))
        cur = by_key.get(key)
        if cur is None:
            cur = {
                "slug": slug,
                "strategy": strategy,
                "side": side,
                "decision_ts": decision_ts,
                "ask": None,
                "limit": None,
                "post_ts": None,
                "ack_ts": None,
                "vwap": None,
                "shares": None,
            }
            by_key[key] = cur
        if _num(row.get("ask")) is not None:
            cur["ask"] = _num(row.get("ask"))
        if _num(row.get("limit")) is not None:
            cur["limit"] = _num(row.get("limit"))
        if event != "signal":
            if _num(row.get("post_ts")) is not None:
                cur["post_ts"] = _num(row.get("post_ts"))
            if _num(row.get("ack_ts")) is not None:
                cur["ack_ts"] = _num(row.get("ack_ts"))
            if _num(row.get("vwap")) is not None:
                cur["vwap"] = _num(row.get("vwap"))
            if _num(row.get("shares")) is not None:
                cur["shares"] = _num(row.get("shares"))
    return list(by_key.values())


def _pick_attempt(attempts: list[dict], outcome: str) -> Optional[dict]:
    side = str(outcome or "").strip().lower()
    same = [row for row in attempts if row.get("side") == side and side]
    pool = same or list(attempts)
    if not pool:
        return None
    return min(pool, key=lambda row: float(row.get("decision_ts") or 0.0))


def compare_fills(attempts: list[dict], fills: list[dict]) -> list[dict]:
    """One case per watched fill in a market we also signaled or ordered.

    The attempt is our earliest signal on the same outcome. When we only
    traded the other outcome, the earliest signal in the market is used
    and ``same_side`` is false. ``us_minus_them_s`` is our signal time
    minus their payload timestamp, so a negative value means we were first.
    Price difference is our price minus their price, and only on the same
    outcome. Our price is the fill VWAP when shares filled, else the ask.
    """
    by_slug: dict[str, list[dict]] = {}
    for attempt in attempts or []:
        slug = str(attempt.get("slug") or "")
        if slug:
            by_slug.setdefault(slug, []).append(attempt)
    cases: list[dict] = []
    seen: set[str] = set()
    for fill in fills or []:
        if not isinstance(fill, dict):
            continue
        slug = str(fill.get("slug") or "")
        pool = by_slug.get(slug) or []
        if not pool:
            continue
        key = fill_key(fill)
        if key in seen:
            continue
        seen.add(key)
        attempt = _pick_attempt(pool, str(fill.get("outcome") or ""))
        if attempt is None:
            continue
        decision_ts = float(attempt["decision_ts"])
        their_ts = _num(fill.get("their_ts"))
        if their_ts is None:
            continue
        our_px, source = _our_price(attempt)
        their_px = _num(fill.get("price"))
        same_side = str(attempt.get("side") or "") == str(fill.get("outcome") or "").strip().lower()
        price_diff = None
        if same_side and our_px is not None and their_px is not None:
            price_diff = our_px - their_px
        recv = _num(fill.get("recv_ts"))
        their_name = str(fill.get("name") or WALLETS.get(str(fill.get("wallet") or ""), ""))
        cases.append(
            {
                "key": f"{key}|{round(decision_ts, 3)}",
                "slug": slug,
                "duration": fill.get("duration") or window_kind(slug),
                "wallet": fill.get("wallet"),
                "name": their_name,
                "outcome": fill.get("outcome"),
                "trade_side": fill.get("trade_side"),
                "their_price": their_px,
                "their_size": _num(fill.get("size")),
                "their_ts": their_ts,
                "their_recv_ts": recv,
                "tx": fill.get("tx") or "",
                "our_strategy": attempt.get("strategy"),
                "our_side": attempt.get("side"),
                "our_decision_ts": decision_ts,
                "our_post_ts": _num(attempt.get("post_ts")),
                "our_ack_ts": _num(attempt.get("ack_ts")),
                "our_price": our_px,
                "our_price_source": source,
                "same_side": same_side,
                "us_minus_them_s": decision_ts - their_ts,
                "us_minus_them_recv_s": None if recv is None else decision_ts - recv,
                "we_first": decision_ts < their_ts,
                "price_diff": price_diff,
            }
        )
    return cases


def _pack_seconds(values: list[float]) -> Optional[dict]:
    if not values:
        return None
    return {
        "n": len(values),
        "median": round(float(statistics.median(values)), 3),
        "p90": round(float(_percentile(values, 0.90) or 0.0), 3),
    }


def _pack_price(values: list[float]) -> Optional[dict]:
    if not values:
        return None
    return {
        "n": len(values),
        "median": round(float(statistics.median(values)), 4),
        "p90": round(float(_percentile(values, 0.90) or 0.0), 4),
    }


def summarize_cases(cases: list[dict]) -> dict:
    """Median and p90 of our signal time minus their fill time.

    ``we_first`` is the share of cases whose signal was strictly earlier.
    Price difference uses same-outcome pairs only.
    """

    def pack(group: list[dict]) -> dict:
        deltas = [float(row["us_minus_them_s"]) for row in group if _num(row.get("us_minus_them_s")) is not None]
        recvs = [float(row["us_minus_them_recv_s"]) for row in group if _num(row.get("us_minus_them_recv_s")) is not None]
        prices = [float(row["price_diff"]) for row in group if _num(row.get("price_diff")) is not None]
        first = sum(1 for row in group if row.get("we_first"))
        n = len(group)
        return {
            "n": n,
            "we_first": (first / n) if n else None,
            "us_minus_them_s": _pack_seconds(deltas),
            "us_minus_them_recv_s": _pack_seconds(recvs),
            "price_diff": _pack_price(prices),
        }

    by_name: dict[str, list[dict]] = {}
    for row in cases or []:
        by_name.setdefault(str(row.get("name") or "?"), []).append(row)
    return {
        "all": pack(list(cases or [])),
        "by_wallet": {name: pack(rows) for name, rows in sorted(by_name.items())},
    }


def head_to_head(rows: list[dict]) -> tuple[list[dict], dict]:
    """Join ``wallet_fill`` rows to our signals. Returns cases and the summary."""
    fills = []
    n_fills = 0
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict) or row.get("event") != "wallet_fill":
            continue
        key = fill_key(row)
        if key in seen:
            continue
        seen.add(key)
        n_fills += 1
        fills.append(row)
    cases = compare_fills(attempts_from_rows(rows), fills)
    summary = summarize_cases(cases)
    summary["fills"] = n_fills
    return cases, summary


class WalletTape:
    """RTDS activity/trades socket. The callback sees accepted fills only."""

    def __init__(
        self,
        url: str = ACTIVITY_URL,
        *,
        on_fill: Optional[Callable[[dict], None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.url = url
        self.on_fill = on_fill
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Any = None
        self._error = ""
        self._last_msg = 0.0
        self.fills = 0

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lockbot-wallets", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                return

    def last_error(self) -> str:
        with self._lock:
            return self._error

    def age_s(self, now: float) -> Optional[float]:
        with self._lock:
            if self._last_msg <= 0:
                return None
            return float(now) - self._last_msg

    def handle_message(self, raw: Any, *, recv_ts: Optional[float] = None) -> int:
        """Parse one frame. Returns how many new watched fills were delivered."""
        payloads = parse_activity_frame(raw)
        if payloads is None:
            return 0
        recv = float(self._clock() if recv_ts is None else recv_ts)
        with self._lock:
            self._last_msg = recv
            self._error = ""
        delivered = 0
        for payload in payloads:
            fill = normalize_wallet_fill(payload, recv_ts=recv)
            if fill is None:
                continue
            key = fill_key(fill)
            with self._lock:
                if key in self._seen:
                    continue
                self._seen.add(key)
                self._seen_order.append(key)
                while len(self._seen_order) > 10000:
                    self._seen.discard(self._seen_order.popleft())
                self.fills += 1
            delivered += 1
            if self.on_fill is None:
                continue
            try:
                self.on_fill(fill)
            except Exception as exc:
                self._set_error(str(exc)[:200])
        return delivered

    def _run(self) -> None:
        import websocket

        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    self.url,
                    on_open=lambda sock: sock.send(json.dumps(SUBSCRIBE_FRAME)),
                    on_message=lambda _sock, message: self.handle_message(message),
                    on_error=lambda _sock, err: self._set_error(str(err)[:200]),
                )
                self._ws = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self._set_error(str(exc)[:200])
            self._ws = None
            if self._stop.is_set():
                return
            self._stop.wait(2.0)

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._error = message
