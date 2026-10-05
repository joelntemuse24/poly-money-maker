"""FAK buy dispatch. Dry-run never touches the CLOB client."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Optional

from buy.lock_fair import taker_fee
from buy.lock_gates import buy_spent_usd, execute_buy
from buy.sister_bid import buy_matched_shares


def post_fak_buy(client: Any, plan: dict) -> dict:
    """Sign and post one FAK buy. ``amount`` is USDC notional, limit is the ask."""
    from py_clob_client_v2 import MarketOrderArgs, OrderType
    from py_clob_client_v2.order_builder.constants import BUY

    build_ts = time.time()
    args = MarketOrderArgs(
            token_id=str(plan["token_id"]),
            amount=float(plan["notional"]),
            side=BUY,
            price=float(plan["limit"]),
            order_type=OrderType.FAK,
        )
    sign_ts = time.time()
    signed = client.create_market_order(args)
    signed_ts = time.time()
    post_ts = time.time()
    result = client.post_order(signed, order_type=OrderType.FAK)
    if not isinstance(result, dict):
        result = {"raw": str(result)[:500]}
    ack_ts = time.time()
    result.update(order_build_ts=build_ts, sign_ts=sign_ts, signed_ts=signed_ts, http_send_ts=post_ts, response_ts=ack_ts, confirm_ts=ack_ts)
    result["post_ts"] = post_ts
    result["ack_ts"] = ack_ts
    result["post_to_ack_ms"] = (ack_ts - post_ts) * 1000.0
    result["decision_to_order_ms"] = result["post_to_ack_ms"]
    result["posted"] = True
    return result


def fak_no_match(value: Any) -> bool:
    """Recognize an unfilled FAK without treating it as an execution failure."""
    text = str(value).lower()
    if isinstance(value, dict) and str(value.get("status", "")).lower() in {"unmatched", "canceled", "cancelled"}:
        return float(value.get("takingAmount") or 0) == 0 and not value.get("orderID")
    return any(token in text for token in ("no orders found", "no match", "unfilled", "not filled"))


def kill_failure(value: Any) -> bool:
    """Failures that may safely contribute to an external kill policy."""
    text = str(value).lower()
    return any(token in text for token in ("auth", "unauthorized", "invalid signature", "oversize", "cap_recheck"))


def dispatch_buy(plan: dict, *, dry_run: bool, client: Any = None) -> dict:
    """Paper-fill against the in-memory book, or post a live FAK.

    ``client`` is not read when ``dry_run`` is true.
    """
    started = time.perf_counter()
    if dry_run:
        fill = execute_buy(plan, dry_run=True, poster=None)
    else:
        if client is None:
            raise RuntimeError("live buy requires a clob client")
        fill = execute_buy(plan, dry_run=False, poster=lambda item: post_fak_buy(client, item))
    fill.setdefault("decision_to_order_ms", (time.perf_counter() - started) * 1000.0)
    return fill


def normalize_fill(fill: dict, plan: dict, cfg: dict) -> dict:
    """Shares, dollars, and the modelled taker fee for one buy result."""
    if fill.get("dry_run"):
        shares = float(fill.get("shares") or 0.0)
        cost = float(fill.get("cost") or 0.0)
        vwap = fill.get("vwap")
    else:
        shares = buy_matched_shares(fill, float(plan.get("shares") or 0.0))
        cost = buy_spent_usd(fill, float(plan.get("notional") or 0.0))
        vwap = (cost / shares) if shares > 0 and cost > 0 else plan.get("limit")
    price = float(vwap if vwap else plan.get("limit") or 0.0)
    fee = taker_fee(
        price,
        float(cfg.get("taker_fee_rate") or 0.07),
        float(cfg.get("taker_fee_exponent") or 1.0),
    ) * shares
    out = {
        "shares": shares,
        "cost": cost,
        "vwap": vwap,
        "fee": fee,
        "posted": bool(fill.get("posted")),
        "dry_run": bool(fill.get("dry_run")),
        "decision_to_order_ms": fill.get("decision_to_order_ms"),
        "raw_status": fill.get("status") or fill.get("errorMsg") or fill.get("error"),
    }
    for key in (
        "order_build_ts",
        "sign_ts",
        "signed_ts",
        "http_send_ts",
        "response_ts",
        "confirm_ts",
        "decision_ts",
        "post_ts",
        "ack_ts",
        "binance_recv_ts",
        "book_recv_ts",
        "decision_to_post_ms",
        "post_to_ack_ms",
        "recv_to_decision_ms",
        "recv_to_post_ms",
        "reason",
    ):
        if key in fill and fill.get(key) is not None:
            out[key] = fill.get(key)
    return out


def open_live_client(builder: Callable[[], Optional[Any]] | None = None) -> tuple[Optional[Any], str]:
    """Build the order client. A failure string means do not post.

    The process env is loaded at startup. A hot reload can call this, but
    it cannot see a ``.env`` that was not already in the environment. When
    the string is set, the operator restarts the process.
    """
    build = build_clob_client if builder is None else builder
    try:
        client = build()
    except Exception as exc:
        return None, str(exc)[:200]
    if client is None:
        return None, "missing PRIVATE_KEY or FUNDER_ADDRESS; restart after the env is loaded"
    return client, ""


class LivePoster:
    """Posts live orders off the decision thread. The queue is the hand-off."""

    def __init__(self, handler: Callable[[Any], None]) -> None:
        self._handler = handler
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lockbot-orders", daemon=True)
        self._thread.start()

    def submit(self, job: Any) -> None:
        self._queue.put(job)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._queue.get()
            if job is None:
                return
            try:
                self._handler(job)
            except Exception:
                continue


def build_clob_client() -> Optional[Any]:
    """Mintbot's signing setup, constructed only for a live run."""
    import os

    private_key = os.getenv("PRIVATE_KEY") or ""
    funder = os.getenv("FUNDER_ADDRESS") or ""
    if not private_key or not funder:
        return None
    from py_clob_client_v2 import ApiCreds, ClobClient

    host = "https://clob.polymarket.com"
    chain_id = int(os.getenv("CHAIN_ID") or 137)
    api_key = os.getenv("API_KEY") or ""
    api_secret = os.getenv("API_SECRET") or ""
    api_passphrase = os.getenv("API_PASSPHRASE") or ""
    if api_key and api_secret and api_passphrase:
        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    else:
        creds = ClobClient(host=host, key=private_key, chain_id=chain_id).create_or_derive_api_key()
    return ClobClient(
        host=host,
        key=private_key,
        chain_id=chain_id,
        creds=creds,
        signature_type=1,
        funder=funder,
    )


def warm_market(client: Any, condition_id: str) -> None:
    """Cache tick size, neg-risk, and fee so the hot path does not HTTP.

    ``create_market_order`` calls ``__ensure_market_info_cached``, which
    fetches those three unless ``get_clob_market_info`` already ran for
    this condition. Price and size still change every clip, so the order
    itself is signed at decision time.
    """
    if client is None or not condition_id:
        return
    getter = getattr(client, "get_version", None)
    if callable(getter):
        try:
            getter()
        except Exception:
            pass
    info = getattr(client, "get_clob_market_info", None)
    if not callable(info):
        return
    info(str(condition_id))
