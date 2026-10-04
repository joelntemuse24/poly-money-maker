"""Dry-run fills walked on the book after the measured latency, not at the decision."""

from __future__ import annotations

import time
from typing import Any, Optional

from buy.lock_gates import simulate_fak_buy


def enqueue_paper(queue: list[dict], decision: dict, *, now: float) -> dict:
    """Remember a signal. The fill is not walked until ``latency_s`` has passed."""
    item = {
        "decision": decision,
        "decision_ts": float(now),
        "asks_at_decision": list(decision.get("asks") or []),
        "ask": decision.get("ask"),
        "limit": decision.get("limit"),
        "notional": float(decision.get("notional") or 0.0),
        "strategy": decision.get("strategy"),
        "slug": decision.get("slug"),
        "side": decision.get("side"),
        "token_id": decision.get("token_id"),
        "binance_recv_ts": decision.get("binance_recv_ts"),
        "book_recv_ts": decision.get("book_recv_ts"),
    }
    queue.append(item)
    return item


def take_due(queue: list[dict], now: float, latency_s: float) -> tuple[list[dict], list[dict]]:
    """Split the queue into clips whose latency has elapsed, and the rest."""
    due: list[dict] = []
    keep: list[dict] = []
    limit = float(now) - float(latency_s)
    for item in queue:
        if float(item.get("decision_ts") or 0.0) <= limit:
            due.append(item)
        else:
            keep.append(item)
    return due, keep


def walk_late_book(item: dict, asks: list, *, now: float) -> dict:
    """Fill against the book as it stands ``now``, after the decision.

    ``post_ts`` is when the walk starts and ``ack_ts`` is when it finishes.
    A book that ran through the limit fills zero shares and does not count
    as a side lock.
    """
    post_ts = time.time()
    fill = simulate_fak_buy(asks or [], float(item["limit"]), float(item["notional"]))
    ack_ts = time.time()
    decision_ts = float(item["decision_ts"])
    binance_recv = item.get("binance_recv_ts")
    fill.update(
        {
            "dry_run": True,
            "posted": False,
            "strategy": item.get("strategy"),
            "slug": item.get("slug"),
            "side": item.get("side"),
            "token_id": item.get("token_id"),
            "decision_ts": decision_ts,
            "post_ts": post_ts,
            "ack_ts": ack_ts,
            "binance_recv_ts": binance_recv,
            "book_recv_ts": item.get("book_recv_ts"),
            "decision_to_post_ms": (post_ts - decision_ts) * 1000.0,
            "post_to_ack_ms": (ack_ts - post_ts) * 1000.0,
            "ask_at_decision": item.get("ask"),
            "notional_planned": item.get("notional"),
        }
    )
    if binance_recv is not None:
        try:
            fill["recv_to_decision_ms"] = (decision_ts - float(binance_recv)) * 1000.0
            fill["recv_to_post_ms"] = (post_ts - float(binance_recv)) * 1000.0
        except (TypeError, ValueError):
            pass
    if float(fill.get("shares") or 0.0) <= 0:
        fill["reason"] = "book_moved"
    else:
        fill["reason"] = "paper_fill"
    return fill


def latency_summary(rows: list[dict]) -> dict:
    """Percentiles of decision-to-post and receive-to-post, in milliseconds."""
    decision = []
    recv = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        # ``fill`` repeats the timestamps already on ``paper_fill`` / ``entry``.
        if row.get("event") not in {"signal", "paper_fill", "entry"}:
            continue
        for key, bucket in (("decision_to_post_ms", decision), ("recv_to_post_ms", recv)):
            value = row.get(key)
            if value is None:
                continue
            try:
                bucket.append(float(value))
            except (TypeError, ValueError):
                continue

    def pack(values: list[float]) -> Optional[dict]:
        if not values:
            return None
        ordered = sorted(values)
        def pct(p: float) -> float:
            idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
            return round(ordered[idx], 3)
        return {"n": len(ordered), "p50": pct(0.50), "p95": pct(0.95), "max": round(ordered[-1], 3)}

    return {"decision_to_post_ms": pack(decision), "recv_to_post_ms": pack(recv)}
