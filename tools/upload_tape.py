#!/usr/bin/env python3
"""Upload oldest tape days over 5GB, and days that are at least 7 days old.

A day is deleted locally only after rclone check (or size plus MD5)
matches. A failed upload keeps the files. This process is the only
place that runs rclone for the tape cap. It does not place orders.

    python tools/upload_tape.py

Remote: POLY_TAPE_REMOTE or deploy/tape_remote.path (for example
``gdrive:poly-tape``). See deploy/RECORDER.md for the headless
Google Drive auth steps.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.upload import resolve_tape_remote, subprocess_runner, upload_over_cap  # noqa: E402


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"event": "tape_upload_already_running"}), flush=True)
        raise SystemExit(0)
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload oldest tape days over the 5GB cap")
    parser.add_argument("--repo", default=str(ROOT))
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    _acquire_lock(repo / ".tape-upload.lock")
    remote = resolve_tape_remote(repo)
    result = upload_over_cap(repo, remote, subprocess_runner)
    print(json.dumps({"ok": result.get("ok"), "error": result.get("error"), "deleted": result.get("deleted"), "tape_bytes": result.get("tape_bytes")}))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
