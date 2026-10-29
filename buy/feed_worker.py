"""Child entry point for ``feed_process``: ``python -m buy.feed_worker``.

A plain subprocess instead of a ``multiprocessing`` child, so the child never
re-imports the bot: no ``mintbot`` module state, no inherited threads, no lock
that could be held by the parent. It imports only ``buy.feed_process`` and
``buy.binance_trades``.

The parent writes the (non-secret) feed config to stdin as one JSON object and
passes the shared-memory name, layout parameters and the eventfd number on argv.
"""
from __future__ import annotations

import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 6:
        sys.stderr.write(
            "usage: feed_worker SHM_NAME SOURCES CAPACITY TRIGGERS WAKE_FD STATUS_PERIOD\n"
        )
        return 2
    shm_name, sources, capacity, triggers, wake_fd, period = args
    from buy.feed_process import FeedWorker, Layout, SharedBlock

    layout = Layout(sources=int(sources), capacity=int(capacity), triggers=int(triggers))
    try:
        payload = sys.stdin.read()
        cfg = json.loads(payload) if payload.strip() else {}
    except Exception:
        cfg = {}
    cfg["feed_process_status_s"] = float(period)
    block = SharedBlock(layout.total_bytes, create=False, name=shm_name)
    try:
        return FeedWorker(cfg, block, layout, int(wake_fd)).run()
    finally:
        block.close()


if __name__ == "__main__":
    raise SystemExit(main())
