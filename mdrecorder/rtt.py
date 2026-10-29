"""Latency probe: python -m mdrecorder.rtt.

Measures TCP connect time and TLS handshake time for every websocket
endpoint the recorder uses, takes the median of N samples, and prints a
table. Each sample is a fresh connection, so the TLS time excludes TCP.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import statistics
import sys
import time
from urllib.parse import urlsplit

from .streams import endpoints

DEFAULT_SAMPLES = 5
TIMEOUT_S = 5.0


def _target(url):
    parts = urlsplit(url)
    secure = parts.scheme == "wss"
    host = parts.hostname
    port = parts.port or (443 if secure else 80)
    return host, port, secure


def _measure(host, port, secure, samples):
    tcp_times = []
    tls_times = []
    errors = []
    for _ in range(samples):
        sock = None
        try:
            start = time.perf_counter()
            sock = socket.create_connection((host, port), timeout=TIMEOUT_S)
            tcp_times.append((time.perf_counter() - start) * 1000.0)
            sock.settimeout(TIMEOUT_S)
            if secure:
                ctx = ssl.create_default_context()
                tls_start = time.perf_counter()
                sock = ctx.wrap_socket(sock, server_hostname=host)
                tls_times.append((time.perf_counter() - tls_start) * 1000.0)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    tcp_med = statistics.median(tcp_times) if tcp_times else None
    tls_med = statistics.median(tls_times) if tls_times else None
    return tcp_med, tls_med, len(errors), samples, errors


def _fmt(value):
    return "-" if value is None else f"{value:.1f}"


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="mdrecorder.rtt",
        description="Measure TCP connect and TLS handshake latency per websocket endpoint",
    )
    parser.add_argument(
        "--samples", type=int, default=DEFAULT_SAMPLES, help="samples per endpoint (default 5)"
    )
    parser.add_argument(
        "--streams", default=None, help="comma-separated stream names to probe (default: all)"
    )
    args = parser.parse_args(argv)
    if args.samples < 1:
        parser.error("--samples must be at least 1")
    return args


def main(argv=None):
    args = _parse_args(argv)
    only = None
    if args.streams:
        only = {part.strip() for part in args.streams.split(",") if part.strip()}
        if not only:
            raise SystemExit("--streams needs at least one stream name")

    rows = []
    for url, names in endpoints():
        if only is not None and not only.intersection(names):
            continue
        host, port, secure = _target(url)
        tcp, tls, failed, total, errors = _measure(host, port, secure, args.samples)
        rows.append({
            "url": url,
            "names": ",".join(names),
            "host": f"{host}:{port}",
            "tcp": tcp,
            "tls": tls,
            "note": "" if not failed else f"{failed}/{total} failed: {errors[0][:60]}",
        })

    if not rows:
        print("no matching endpoints", file=sys.stderr)
        return 2

    name_w = max(len("stream"), *(len(r["names"]) for r in rows))
    host_w = max(len("host"), *(len(r["host"]) for r in rows))
    header = (
        f"{'stream':<{name_w}}  {'host':<{host_w}}  {'tcp_ms':>8}  {'tls_ms':>8}  "
        f"{'samples':>7}  note"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['names']:<{name_w}}  {row['host']:<{host_w}}  "
            f"{_fmt(row['tcp']):>8}  {_fmt(row['tls']):>8}  "
            f"{args.samples:>7}  {row['note']}"
        )
    print()
    print(f"median of {args.samples} fresh connections per endpoint; TLS excludes TCP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
