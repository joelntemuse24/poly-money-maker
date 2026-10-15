#!/usr/bin/env python3
"""Record full CLOB depth for the current and next BTC 15m market.

No orders and no credentials. A separate process from mintbot.

    python recorder.py
    python recorder.py --seconds 60

Stop with SIGTERM or by touching STOP_RECORDER.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"event": "recorder_already_running"}), flush=True)
        raise SystemExit(0)
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BTC 15m full-depth tape (no orders)")
    parser.add_argument("--tape-dir", default=str(ROOT / "data" / "tape"))
    parser.add_argument("--stop-file", default=str(ROOT / "STOP_RECORDER"))
    parser.add_argument("--seconds", type=float, default=0.0, help="exit after N seconds (0 = until stopped)")
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    args = parser.parse_args(argv)
    _acquire_lock(ROOT / ".recorder.lock")
    from recorder.run import run

    return run(
        tape_dir=args.tape_dir,
        stop_file=args.stop_file,
        seconds=args.seconds,
        gamma_url=args.gamma_url,
    )


if __name__ == "__main__":
    sys.exit(main())
