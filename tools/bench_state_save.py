#!/usr/bin/env python3
"""Compare synchronous atomic saves with detached background submissions."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buy.mint_loops import persist_digest
from buy.state_writer import StateWriter, atomic_write, snapshot_state


def synthetic_state(count):
    intent = {
        "status": "completed", "created_at": 1700000000, "updated_at": 1700001000,
        "start_ts": 1700000100, "end_ts": 1700001000, "shares": 200.0,
        "condition_id": "0x" + "a" * 64, "slug": "btc-updown-15m-1700000100",
        "up_token": "1" * 76, "dn_token": "2" * 76, "transaction_id": "0x" + "b" * 64,
        "question": "Bitcoin Up or Down - October 16, 12:00PM-12:15PM ET",
        "sold_loser": True, "sold_winner": True, "sold_dump": True, "redeemed": True,
    }
    fields = ("sell_filled", "sell_limit", "sell_fill_px", "sell_dump_filled", "sell_dump_limit",
              "sell_dump_fill_px", "sell_scrap_keep", "sell_scrap_target", "sell_attempts",
              "sell_dump_attempts", "observed_up", "observed_dn", "reclaim_filled",
              "reclaim_stop_filled", "reclaim_hedge_filled", "sell_winner_filled")
    for prefix in ("", "last_", "risk_", "book_"):
        for key in fields:
            intent[prefix + key] = 0.123456789
    return {"intents": {f"0x{index:064x}": dict(intent) for index in range(count)}, "redeems": {}}


def measure(count, iterations, directory):
    state = synthetic_state(count)
    size = len(json.dumps(state, separators=(",", ":"), sort_keys=True).encode())
    old, new = [], []
    writer = StateWriter(directory / "background.json")
    for _ in range(iterations):
        started = time.perf_counter()
        atomic_write(directory / "sync.json", state)
        old.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        detached = snapshot_state(state)
        # commit_state also records this digest while holding STATE_LOCK.
        persist_digest(detached)
        seq = writer.submit(detached)
        new.append((time.perf_counter() - started) * 1000)
        if not writer.wait(seq, 10):
            raise RuntimeError("background benchmark save failed")
    if not writer.stop(10):
        raise RuntimeError("background benchmark flush failed")
    print(f"intents={count} fields={len(next(iter(state['intents'].values())))} bytes={size} "
          f"old_ms={statistics.median(old):.3f} snapshot_submit_ms={statistics.median(new):.3f} "
          f"iterations={iterations}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=7)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="bench-state-save-") as tmp:
        for count in (1400, 24):
            measure(count, args.iterations, Path(tmp))


if __name__ == "__main__":
    main()
