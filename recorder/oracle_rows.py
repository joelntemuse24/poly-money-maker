"""BTC reference rows from the same RTDS parsers mintbot uses.

This does not start ``OracleLogService`` and does not write
``logs/oracle_twap.jsonl``. The recorder has its own socket.
"""

from __future__ import annotations

from typing import Any


def oracle_records(raw: Any, recv_ts: float) -> list[dict]:
    """One RTDS frame as tape rows. Empty for pings and other topics."""
    from buy.oracle_log import (
        parse_rtds_live,
        parse_rtds_message,
        parse_rtds_price_snapshot,
    )

    rows: list[dict] = []
    for sample in parse_rtds_message(raw):
        rows.append(
            {
                "event_type": "oracle",
                "source": "twap",
                "symbol": str(sample.symbol),
                "price": str(sample.twap),
                "exch_ts": float(sample.obs_ts),
                "recv_ts": float(recv_ts),
                "leg": "",
            }
        )
    live = parse_rtds_live(raw)
    if live is not None:
        rows.append(_live_row("live", live.price, live.obs_ts, recv_ts))
    for snap in parse_rtds_price_snapshot(raw):
        rows.append(_live_row("live_snapshot", snap.price, snap.obs_ts, recv_ts))
    return rows


def _live_row(source: str, price: Any, obs_ts: float, recv_ts: float) -> dict:
    return {
        "event_type": "oracle",
        "source": source,
        "symbol": "btc/usd",
        "price": str(price),
        "exch_ts": float(obs_ts),
        "recv_ts": float(recv_ts),
        "leg": "",
    }


def stamp_bag(record: dict, slug: str, start_ts: float) -> dict:
    row = dict(record)
    row["slug"] = slug
    row["start_ts"] = float(start_ts)
    row["leg"] = ""
    return row
