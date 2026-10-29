"""Entry point: python -m mdrecorder.run --out DIR."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .core import Recorder


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="mdrecorder.run", description="Standalone BTC market-data recorder"
    )
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument(
        "--max-gb",
        type=float,
        default=None,
        help="disk cap in GB (default: MDREC_MAX_GB or 12)",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--streams",
        default=None,
        help="comma-separated stream names to record (default: all)",
    )
    return parser.parse_args(argv)


def _stream_set(raw):
    if raw is None:
        return None
    names = {part.strip() for part in raw.split(",") if part.strip()}
    if not names:
        raise SystemExit("--streams needs at least one stream name")
    return names


async def _main(args):
    rec = Recorder(args.out, max_gb=args.max_gb, only=_stream_set(args.streams))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, rec.stop_evt.set)
        except (NotImplementedError, RuntimeError):
            pass
    await rec.run_until_stopped()


def main(argv=None):
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
