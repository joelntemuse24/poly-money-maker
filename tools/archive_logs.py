#!/usr/bin/env python3
"""Copy yesterday's bot logs into logs/durable. Does not delete logs.

    python tools/archive_logs.py
    python tools/archive_logs.py --day 2026-10-14

Remote copy: set POLY_ARCHIVE_REMOTE (for example ``gdrive:poly-logs``)
or write that string on one line in deploy/archive_remote.path.
rclone must already be configured for that remote. This job does not
delete the local copy. The recorder may later remove durable tape
days under its disk cap; see deploy/RECORDER.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.archive import archive_day, parse_day, resolve_remote, yesterday  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive one UTC day of poly logs")
    parser.add_argument("--day", default="", help="YYYY-MM-DD, default yesterday UTC")
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--dest", default="", help="default <repo>/logs/durable")
    parser.add_argument("--unit", default="polymintbot")
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    day = parse_day(args.day) if args.day else yesterday()
    dest = Path(args.dest) if args.dest else repo / "logs" / "durable"
    remote = resolve_remote(repo)
    manifest = archive_day(repo, day, dest, unit=args.unit, remote=remote)
    print(json.dumps({"day": manifest["day"], "dest": manifest["dest"], "journal_ok": manifest["journal"].get("ok")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
