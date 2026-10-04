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


DEFAULT_H2H_WINDOW_S = 10.0


def _nearest_attempt(
    attempts: list[dict],
    *,
    slug: str,
    outcome: str,
    their_ts: float,
    window_s: float,
) -> Optional[dict]:
    """Closest same-outcome signal in this market inside the window."""
    side = str(outcome or "").strip().lower()
    if side not in {"up", "down"}:
        return None
    best: Optional[dict] = None
    best_abs = None
    for row in attempts:
        if str(row.get("slug") or "") != slug or str(row.get("side") or "") != side:
            continue
        decision = _num(row.get("decision_ts"))
        if decision is None:
            continue
        gap = abs(decision - their_ts)
        if gap > float(window_s) + 1e-9:
            continue
        if best is None or gap < best_abs - 1e-9 or (abs(gap - best_abs) <= 1e-9 and decision < float(best["decision_ts"])):
            best = row
            best_abs = gap
    return best


def _trigger_fired(attempts: list[dict], fill: dict, move: Optional[float], window_s: float) -> bool:
    """True when an s2 signal fired on this move's side inside the window."""
    if move is None or move == 0:
        return False
    side = "up" if move > 0 else "down"
    slug = str(fill.get("slug") or "")
    their_ts = _num(fill.get("their_ts"))
    if their_ts is None:
        return False
    for row in attempts:
        if str(row.get("slug") or "") != slug:
            continue
        if str(row.get("strategy") or "") != "s2" or str(row.get("side") or "") != side:
            continue
        decision = _num(row.get("decision_ts"))
        if decision is None:
            continue
        if abs(decision - their_ts) <= float(window_s) + 1e-9:
            return True
    return False


def _s2_market(fill: dict) -> bool:
    if "s2_market" in fill:
        return bool(fill.get("s2_market"))
    return str(fill.get("slug") or "").lower().startswith("btc-updown-5m-")


def compare_fills(attempts: list[dict], fills: list[dict], *, window_s: float = DEFAULT_H2H_WINDOW_S) -> list[dict]:
    """One row per watched fill.

    The match is our nearest same-outcome signal within ``window_s`` of
    their payload timestamp. A fill outside that window stays unpaired.
    ``us_minus_them_s`` is our signal time minus theirs, so a negative
    value means we were first. On BTC 5m, ``binance_move`` is the 3-second
    move at their fill and ``trigger_fired`` says an s2 signal took that
    same direction inside the window. ``move_class`` on an unpaired 5m
    fill is ``missed`` when that move clears the s2 threshold and
    ``no_move`` when it does not.
    """
    window = float(window_s)
    cases: list[dict] = []
    seen: set[str] = set()
    for fill in fills or []:
        if not isinstance(fill, dict):
            continue
        key = str(fill.get("fill_id") or fill_key(fill))
        if key in seen:
            continue
        seen.add(key)
        their_ts = _num(fill.get("their_ts"))
        if their_ts is None:
            continue
        slug = str(fill.get("slug") or "")
        attempt = _nearest_attempt(attempts, slug=slug, outcome=str(fill.get("outcome") or ""), their_ts=their_ts, window_s=window)
        move = _num(fill.get("binance_move"))
        move_min = _num(fill.get("move_min"))
        if move_min is None:
            move_min = 2.0
        s2 = _s2_market(fill)
        qualifying = move is not None and abs(move) + 1e-12 >= move_min
        fired = _trigger_fired(attempts, fill, move, window) if s2 and qualifying else False
        paired = attempt is not None
        if not s2:
            move_class = "other" if not paired else "paired"
        elif move is None:
            move_class = "unknown" if not paired else "paired"
        elif abs(move) + 1e-12 >= move_min:
            move_class = "missed" if not paired else "paired"
        else:
            move_class = "no_move" if not paired else "paired"
        their_px = _num(fill.get("price"))
        recv = _num(fill.get("recv_ts"))
        their_name = str(fill.get("name") or WALLETS.get(str(fill.get("wallet") or ""), ""))
        row = {
            "key": key,
            "fill_id": key,
            "paired": paired,
            "slug": slug,
            "duration": fill.get("duration") or window_kind(slug),
            "s2_market": s2,
            "wallet": fill.get("wallet"),
            "name": their_name,
            "outcome": fill.get("outcome"),
            "trade_side": fill.get("trade_side"),
            "their_price": their_px,
            "their_size": _num(fill.get("size")),
            "their_ts": their_ts,
            "their_recv_ts": recv,
            "tx": fill.get("tx") or "",
            "asset": fill.get("asset") or "",
            "binance_move": move,
            "move_min": move_min if s2 else None,
            "trigger_fired": fired,
            "move_class": move_class,
            "h2h_window_s": window,
            "our_strategy": None,
            "our_side": None,
            "our_decision_ts": None,
            "our_post_ts": None,
            "our_ack_ts": None,
            "our_price": None,
            "our_price_source": "",
            "us_minus_them_s": None,
            "us_minus_them_recv_s": None,
            "we_first": None,
            "price_diff": None,
            "same_side": False,
        }
        if attempt is not None:
            decision_ts = float(attempt["decision_ts"])
            our_px, source = _our_price(attempt)
            row.update(
                {
                    "our_strategy": attempt.get("strategy"),
                    "our_side": attempt.get("side"),
                    "our_decision_ts": decision_ts,
                    "our_post_ts": _num(attempt.get("post_ts")),
                    "our_ack_ts": _num(attempt.get("ack_ts")),
                    "our_price": our_px,
                    "our_price_source": source,
                    "us_minus_them_s": decision_ts - their_ts,
                    "us_minus_them_recv_s": None if recv is None else decision_ts - recv,
                    "we_first": decision_ts < their_ts,
                    "price_diff": None if our_px is None or their_px is None else our_px - their_px,
                    "same_side": True,
                }
            )
        cases.append(row)
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
    """Share of their fills we also signalled, and the gap on those pairs.

    The signed gap and the price difference use paired fills only. Unpaired
    BTC 5m fills split into ``missed`` (the 3s move cleared the s2 bar and
    we did not signal) and ``no_move`` (they traded without that move).
    """

    def pack(group: list[dict]) -> dict:
        paired = [row for row in group if row.get("paired")]
        deltas = [float(row["us_minus_them_s"]) for row in paired if _num(row.get("us_minus_them_s")) is not None]
        prices = [float(row["price_diff"]) for row in paired if _num(row.get("price_diff")) is not None]
        n = len(group)
        n_paired = len(paired)
        return {
            "fills": n,
            "paired": n_paired,
            "signalled": (n_paired / n) if n else None,
            "us_minus_them_s": _pack_seconds(deltas),
            "price_diff": _pack_price(prices),
            "unpaired": n - n_paired,
            "missed": sum(1 for row in group if row.get("move_class") == "missed"),
            "no_move": sum(1 for row in group if row.get("move_class") == "no_move"),
            "move_unknown": sum(1 for row in group if row.get("move_class") == "unknown"),
            "other": sum(1 for row in group if row.get("move_class") == "other"),
            "same_move": sum(1 for row in group if row.get("trigger_fired")),
        }

    by_name: dict[str, list[dict]] = {}
    for row in cases or []:
        by_name.setdefault(str(row.get("name") or "?"), []).append(row)
    return {
        "all": pack(list(cases or [])),
        "by_wallet": {name: pack(rows) for name, rows in sorted(by_name.items())},
    }


def _window_from_rows(rows: list[dict]) -> float:
    for row in rows or []:
        if isinstance(row, dict) and row.get("event") == "startup" and _num(row.get("h2h_window_s")) is not None:
            return float(row["h2h_window_s"])
    return DEFAULT_H2H_WINDOW_S


def head_to_head(rows: list[dict], *, window_s: Optional[float] = None) -> tuple[list[dict], dict]:
    """Join ``wallet_fill`` rows to our signals. Returns cases and the summary."""
    stored = list(rows or [])
    window = DEFAULT_H2H_WINDOW_S if window_s is None else float(window_s)
    if window_s is None:
        window = _window_from_rows(stored)
    moves: dict[str, dict] = {}
    for row in stored:
        if not isinstance(row, dict) or row.get("event") not in {"wallet_fill", "wallet_compare"}:
            continue
        if _num(row.get("binance_move")) is None:
            continue
        key = str(row.get("fill_id") or fill_key(row))
        moves[key] = row
    fills = []
    seen: set[str] = set()
    for row in stored:
        if not isinstance(row, dict) or row.get("event") != "wallet_fill":
            continue
        key = str(row.get("fill_id") or fill_key(row))
        if key in seen:
            continue
        seen.add(key)
        fill = dict(row)
        extra = moves.get(key)
        if extra is not None and _num(fill.get("binance_move")) is None:
            fill["binance_move"] = extra.get("binance_move")
            if extra.get("move_min") is not None:
                fill["move_min"] = extra.get("move_min")
            if "s2_market" in extra:
                fill["s2_market"] = extra.get("s2_market")
        fills.append(fill)
    cases = compare_fills(attempts_from_rows(stored), fills, window_s=window)
    summary = summarize_cases(cases)
    summary["fills"] = len(fills)
    summary["window_s"] = window
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
