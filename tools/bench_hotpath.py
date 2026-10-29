#!/usr/bin/env python3
"""Offline latency benchmark of the 5m sniper hot path.

Each stage is timed with ``time.perf_counter_ns`` and reported in microseconds
(p50/p90/p99/max):

1. ``parse_agg_trade(raw)``      - orjson decode + per-exchange extraction
2. ``PriceGrid.add(...)``        - source/recv bookkeeping
3. ``best_jump_in_window(500)``  - every feed against its own sliding window
4. ``jump_in_window(500)``       - primary feed only
5. ``Runner.on_binance_tick``    - threshold -> book sides -> ``_sniper_check(
                                   defer_buy=True)`` -> ``_sniper_prepare``.
                                   ``_sniper_execute`` is stubbed; the POST is
                                   timed as stage 9.
6. ``Strategy._sniper_check``    - standalone on ticks whose jump clears
                                   ``sniper_jump_usd``; ends in ``_sniper_prepare``
                                   (prepares counted)
7. ``PreSignedEntry.get_l2_headers`` - cache hit
8. ``PreSignedEntry.get_l2_headers`` - cache miss (key dropped before the call)
9. ``fast_post_order``           - fake HTTP client returns a 200 body at once,
                                   so only our code is timed (L2 lookup included)

Per-trade JSON logging (``order_latency_fields``) runs after the POST and is not
measured. Thread handoff jitter is out of scope.

The stream is synthetic but production-shaped: the Binance aggTrade primary plus
the six extra feeds at realistic shares, so each per-source ring buffer fills
toward its 4096-entry max. Prices random-walk around 80000 with occasional +-30
USD jumps; the strike trails the implied spot, as the RTDS TWAP does. ``max``
includes GC pauses, page faults and host scheduling stalls.

``--gc on`` keeps the collector enabled; ``--gc freeze`` runs
``gc.collect(); gc.freeze(); gc.disable()`` first. ``gc.callbacks`` pauses are
recorded and reported. ``--impl rust`` builds ``PriceGrid(use_rust=True)`` for
all stages, so ``window_extrema`` / ``best_jump_in_window`` scan the ``polyhot``
tick rings (see ``rust_backend``).

Usage:
    .venv/bin/python tools/bench_hotpath.py --n 50000 --gc freeze
    .venv/bin/python tools/bench_hotpath.py --n 200000 --gc on --json /tmp/bench.json
"""
from __future__ import annotations

import argparse
import base64
import collections
import gc
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from buy.binance_trades import DEFAULT_EXTRA_WS_URLS, PriceGrid, parse_agg_trade  # noqa: E402
from buy.fivemin import DEFAULTS, FiveMinIO, Runner, Strategy, settings  # noqa: E402
from buy.presign import (  # noqa: E402
    PreSignedEntry, fast_l2_headers, fast_post_order, fast_serialize_json,
)

NOW = time.perf_counter_ns
WINDOW_MS = 500.0
JUMP_USD = 25.0
T0_MS = 1_760_000_000_000
STRIKE_LAG = 25  # ticks (~0.6s) the strike trails the tape, like the RTDS TWAP
STAGES = ("parse_agg_trade", "grid_add", "best_jump_in_window", "jump_in_window", "on_binance_tick",
          "sniper_check_prepare", "presign_l2_hit", "presign_l2_miss", "fast_post_order")

# (source key exactly as ``BinanceTrades`` passes it, traffic share, basis vs primary).
_BOOKTICKER, _BYBIT, _COINBASE, _DERIBIT, _KRAKEN, _BITSTAMP = DEFAULT_EXTRA_WS_URLS
FEEDS = (("primary", 0.50, 0.0), (_BOOKTICKER, 0.10, 0.4), (_BYBIT, 0.14, -38.0),
         (_COINBASE, 0.08, 3.0), (_DERIBIT, 0.07, -33.0), (_KRAKEN, 0.06, 1.5),
         (_BITSTAMP, 0.05, 4.0))


def _iso(ex_ms: float) -> str:
    return datetime.fromtimestamp(ex_ms / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _raw(kind: int, ex_ms: float, px: float, seq: int) -> bytes:
    """One wire message in a shape ``parse_agg_trade`` accepts."""
    bid, ask, iso = px - 0.6, px + 0.6, _iso(ex_ms)
    if kind == 0:  # Binance aggTrade
        return (f'{{"e":"aggTrade","E":{ex_ms},"s":"BTCUSDT","a":{seq},"p":"{px:.2f}","q":"0.001",'
                f'"f":{seq},"l":{seq},"T":{ex_ms},"m":true,"M":true}}').encode()
    if kind == 1:  # Binance bookTicker -> mid (spread must be <= $5)
        return (f'{{"u":{seq},"s":"BTCUSDT","b":"{bid:.2f}","B":"0.9","a":"{ask:.2f}",'
                f'"A":"1.2","E":{ex_ms}}}').encode()
    if kind == 2:  # Bybit v5 publicTrade
        return (f'{{"topic":"publicTrade.BTCUSDT","type":"snapshot","ts":{ex_ms},"data":[{{"T":{ex_ms},'
                f'"s":"BTCUSDT","S":"Buy","v":"0.002","p":"{px:.1f}","i":"{seq:x}","BT":false}}]}}').encode()
    if kind == 3:  # Coinbase ticker with best_bid/best_ask/time
        return (f'{{"type":"ticker","sequence":{seq},"product_id":"BTC-USD","price":"{px:.2f}",'
                f'"best_bid":"{bid:.2f}","best_bid_size":"0.5","best_ask":"{ask:.2f}",'
                f'"best_ask_size":"0.7","time":"{iso}"}}').encode()
    if kind == 4:  # Deribit trades (timestamp in ms)
        return ('{"jsonrpc":"2.0","method":"subscription","params":{"channel":"trades.BTC-PERPETUAL.raw",'
                f'"data":[{{"trade_id":"BTC-{seq}","instrument_name":"BTC-PERPETUAL","timestamp":{ex_ms},'
                f'"price":{px:.1f},"amount":10.0,"direction":"buy","tick_direction":0}}]}}}}').encode()
    if kind == 5:  # Kraken v2 trade (ISO timestamp)
        return (f'{{"channel":"trade","type":"update","data":[{{"symbol":"BTC/USD","side":"buy",'
                f'"price":{px:.1f},"qty":0.0011,"ord_type":"market","trade_id":{seq},'
                f'"timestamp":"{iso}"}}]}}').encode()
    # Bitstamp live_trades_btcusd; microtimestamp is microseconds since epoch
    return (f'{{"event":"trade","channel":"live_trades_btcusdt","data":{{"id":{seq},'
            f'"timestamp":"{ex_ms // 1000:.0f}","microtimestamp":"{ex_ms * 1000:.0f}","amount":0.001,'
            f'"amount_str":"0.001","price":{px:.2f},"price_str":"{px:.2f}","type":0}}}}').encode()


def generate(n: int, seed: int = 7):
    """Return [(source, exchange_ms, recv_ms, raw_bytes)] of length ``n``."""
    rnd = random.Random(seed)
    picks = rnd.choices(range(len(FEEDS)), weights=[feed[1] for feed in FEEDS], k=n)
    out, mid, ex_ms = [], 80000.0, T0_MS
    for seq in range(n):
        ex_ms += rnd.randint(8, 40)
        mid += rnd.gauss(0.0, 1.0) - 0.03 * (mid - 80000.0)  # small drift, mean reversion
        if rnd.random() < 0.002:
            mid += 30.0 if rnd.random() < 0.5 else -30.0
        source, _share, basis = FEEDS[picks[seq]]
        px = mid + basis + rnd.gauss(0.0, 0.2)
        out.append((source, float(ex_ms), ex_ms + rnd.uniform(0.5, 3.0), _raw(picks[seq], ex_ms, px, seq)))
    return out


# ---------------------------------------------------------------- backends

class PyBackend:
    name = "py"
    use_rust = False
    parse = staticmethod(parse_agg_trade)
    add = staticmethod(PriceGrid.add)
    best = staticmethod(PriceGrid.best_jump_in_window)
    jump = staticmethod(PriceGrid.jump_in_window)


def rust_backend():
    """``PriceGrid(use_rust=True)``: the window scan runs in ``polyhot.TickRing``.

    Every stage still goes through ``PriceGrid``; only the ``min``/``max`` scan
    over ``window_extrema`` and ``best_jump_in_window`` moves to Rust. Errors out
    when the extension is missing so a Rust run never silently measures Python.
    """
    try:
        import polyhot  # type: ignore
    except Exception as exc:
        raise SystemExit(f"--impl rust: module polyhot is not importable ({exc})")
    if not hasattr(polyhot, "TickRing"):
        raise SystemExit("--impl rust: polyhot is missing TickRing")
    if not PriceGrid(use_rust=True).rust_enabled:
        raise SystemExit("--impl rust: PriceGrid could not enable the Rust window scan")
    return SimpleNamespace(name="rust", use_rust=True, parse=parse_agg_trade,
                           add=PriceGrid.add, best=PriceGrid.best_jump_in_window,
                           jump=PriceGrid.jump_in_window)


# ---------------------------------------------------------------- fixtures

_TOP = {"up": (0.92, 0.94, [[0.92, 20.0]], [[0.94, 25.0], [0.95, 12.0]]),
        "dn": (0.06, 0.08, [[0.06, 20.0]], [[0.08, 25.0]])}
_QUOTES = {side: {"bid": bid, "ask": ask, "bids": bids, "asks": asks, "src": "ws", "age_ms": 40.0}
           for side, (bid, ask, bids, asks) in _TOP.items()}


class _Books:
    """Fake CLOB book feed: fresh top-of-book for both tokens, no I/O."""

    def __init__(self):
        self._top = {f"tok_{side}": SimpleNamespace(bid=b, ask=a, bids=bs, asks=as_, age=0.04)
                     for side, (b, a, bs, as_) in _TOP.items()}

    def usable(self, token):
        return self._top.get(token)

    def source(self, _token):
        return "ws"


def _cfg() -> dict:
    return settings({**DEFAULTS, "fivemin_enabled": True, "sniper_5m_enabled": True,
                     "fivemin_dry_run": False, "sniper_event_driven": True,
                     "sniper_jump_usd": JUMP_USD, "sniper_window_ms": WINDOW_MS,
                     "sniper_min_gap_usd": 20.0, "sniper_max_ask": 0.97, "sniper_min_ask": 0.50,
                     "sniper_ttm_min_s": 15, "sniper_ttm_max_s": 240, "sniper_shares": 10,
                     "sniper_use_offset": True, "sniper_extra_ws_enabled": True,
                     "sniper_max_book_age_ms": 1500.0})


def _setup(now_s, use_rust=False):
    """Minimal Strategy + Runner wired like the fivemin tests: fake IO/books/clock."""
    clock = lambda: now_s[0]  # noqa: E731 - one mutable clock for strategy and runner
    io = FiveMinIO(
        buy=lambda *_a, **_k: {"shares": 10.0, "cost": 9.4, "price": 0.94, "status": "matched"},
        sell=lambda *_a, **_k: {"shares": 0, "proceeds": 0.0, "status": "matched"},
        reserves=lambda _cfg: 0.0, redeem=lambda *_a: None, save=lambda _state: None,
        log=lambda *_a, **_k: None, alert=lambda *_a, **_k: None, clock=clock,
    )
    strat = Strategy(io, startup_ts=0.0)
    start = int(now_s[0])
    market = SimpleNamespace(condition_id="cid1", slug=f"btc-up-or-down-5m-{start}",
                             start_ts=start, end_ts=start + 300, up_token="tok_up",
                             dn_token="tok_dn", active=True, closed=False, neg_risk=False)
    strat.add_market(market, now=now_s[0])
    row = strat.state["markets"]["cid1"]
    row["offset"] = 0.0
    strat.sample_cash(now_s[0], 500.0)
    # The order POST is stage 9; keep the decision timing free of submission.
    strat._sniper_execute = lambda *_a, **_k: None  # type: ignore[method-assign]
    grid = PriceGrid(use_rust=True) if use_rust else PriceGrid()
    binance = SimpleNamespace(start=lambda: None, stop=lambda: None, _on_tick=None)
    runner = Runner(strat, _cfg, None, None, "0xfunder", grid, binance, None, _Books(), clock=clock)
    runner.current = row
    return strat, runner, row, grid


def run_ticks(messages, backend):
    """One pass over the stream; returns per-stage ns samples and counters."""
    cfg = _cfg()
    now_s = [messages[0][2] / 1000.0]
    strat, runner, row, grid = _setup(now_s, use_rust=backend.use_rust)
    parse, add, best, jump = backend.parse, backend.add, backend.best, backend.jump
    samples = {name: [] for name in STAGES[:6]}
    s_parse, s_add, s_best, s_jump, s_tick, s_prep = (samples[k] for k in STAGES[:6])
    counters = {"messages": 0, "parsed": 0, "triggers": 0, "prepared": 0}
    threshold, trailing = JUMP_USD - 1e-9, collections.deque(maxlen=STRIKE_LAG)
    for source, ex_ms, recv_ms, raw in messages:
        t = now_s[0] = recv_ms / 1000.0
        row["start"], row["end"] = t - 100.0, t + 200.0
        row["sniper_5m"] = None
        counters["messages"] += 1
        if counters["messages"] % 1000 == 0:
            strat.sample_cash(t, 500.0)

        t0 = NOW()
        parsed = parse(raw)
        s_parse.append(NOW() - t0)
        if parsed is None:
            continue
        counters["parsed"] += 1
        px = parsed[1]

        t0 = NOW()
        add(grid, parsed[0], px, source=source, recv_ms=recv_ms, recv_wall_ms=recv_ms)
        s_add.append(NOW() - t0)

        t0 = NOW()
        info = best(grid, WINDOW_MS)
        s_best.append(NOW() - t0)

        t0 = NOW()
        jump(grid, WINDOW_MS)
        s_jump.append(NOW() - t0)

        if info is not None:
            trailing.append(info["spot"])
            row["strike"] = trailing[0]  # strike trails implied spot, like the RTDS TWAP
        t0 = NOW()
        runner.on_binance_tick(parsed[0], px)
        s_tick.append(NOW() - t0)

        if info is None or max(info["up_jump"], info["down_jump"]) < threshold:
            continue
        counters["triggers"] += 1
        row["sniper_5m"] = None  # the runner may have prepared on the tick above
        t0 = NOW()
        prepared = strat._sniper_check(cfg, t, row, _QUOTES, grid, row["end"] - t, defer_buy=True)
        s_prep.append(NOW() - t0)
        counters["prepared"] += int(prepared is not None)
        row["sniper_5m"] = None
    return samples, counters


class _FakeResponse:
    """200 with a matched body; headers and elapsed mimic an httpx response."""
    status_code, headers, elapsed = 200, {}, None
    content = b'{"status":"matched","success":true}'


class _FakeHTTP:
    def post(self, url, headers=None, content=None):
        return _FakeResponse()


class ClobClient:  # name matters: fast_post_order checks type(client).__name__
    host = "https://clob.polymarket.com"
    creds = None
    signer = SimpleNamespace(address=lambda: "0xbench")


def run_order_stages(n):
    """Time L2 header cache hit/miss and fast_post_order against a fake client."""
    secret = base64.urlsafe_b64encode(b"bench_secret_bytes_0123456789abc").decode()
    creds = SimpleNamespace(api_key="bench-key", api_secret=secret, api_passphrase="bench-pass")
    payload = {"order": {"salt": 123456789, "makerAmount": "9700000", "takerAmount": "10000000",
                         "tokenId": "1234", "side": "BUY", "signature": "0xdeadbeef"},
               "owner": "bench-key", "orderType": "FAK", "deferExec": True}
    body = fast_serialize_json(payload)
    entry = PreSignedEntry(token_id="tok", side="BUY", amount=9.7, price=0.97, order_type="FAK",
                           signed_order=payload["order"], order_payload=payload,
                           serialized_body=body, serialized_bytes=body.encode("utf-8"),
                           created_at=time.monotonic(), client_id=1,
                           signer_address="0xbench", creds=creds)
    base_sec = int(time.time())
    for sec in range(base_sec, base_sec + 600):  # pre-warm like the live header warmer
        entry.l2_headers_by_sec[sec] = fast_l2_headers("0xbench", creds, "POST", "/order", body, sec)
    client = ClobClient()
    client.creds = creds
    addr, http, order = client.signer.address(), _FakeHTTP(), payload["order"]
    miss_sec = base_sec + 1_000_000  # never warmed -> always a miss
    calls = (("presign_l2_hit", lambda: entry.get_l2_headers(addr, creds, now_sec=base_sec)),
             ("presign_l2_miss", lambda: entry.get_l2_headers(addr, creds, now_sec=miss_sec)),
             ("fast_post_order", lambda: fast_post_order(client, order, entry=entry,
                                                         order_type="FAK", http_client=http)))
    samples = {}
    for name, call in calls:
        out = samples[name] = [0] * n
        for i in range(n):
            if name == "presign_l2_miss":
                entry.l2_headers_by_sec.pop(miss_sec, None)  # outside the timed region
            t0 = NOW()
            call()
            out[i] = NOW() - t0
    return samples


# ---------------------------------------------------------------- reporting

def _pct(ordered_ns, p):
    if not ordered_ns:
        return None
    return ordered_ns[min(len(ordered_ns) - 1, round((p / 100.0) * (len(ordered_ns) - 1)))] / 1000.0


def summarize(samples):
    out = {}
    for name in STAGES:
        ordered = sorted(samples.get(name, ()))
        out[name] = {"n": len(ordered), "p50_us": _pct(ordered, 50), "p90_us": _pct(ordered, 90),
                     "p99_us": _pct(ordered, 99), "max_us": _pct(ordered, 100)}
    return out


def _fmt(value):
    return "-" if value is None else f"{value:.3f}"


def print_table(summary):
    print(f"\n{'stage':<22}{'n':>9}{'p50 us':>11}{'p90 us':>11}{'p99 us':>11}{'max us':>12}")
    print("-" * 76)
    for name in STAGES:
        row = summary[name]
        print(f"{name:<22}{row['n']:>9}{_fmt(row['p50_us']):>11}{_fmt(row['p90_us']):>11}"
              f"{_fmt(row['p99_us']):>11}{_fmt(row['max_us']):>12}")
    print("(max includes GC pauses, page faults and host scheduling stalls)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark the 5m sniper hot path offline.")
    parser.add_argument("--n", type=int, default=200_000, help="number of synthetic messages")
    parser.add_argument("--gc", choices=("on", "freeze"), default="on",
                        help="collector on, or gc.collect()+gc.freeze()+gc.disable()")
    parser.add_argument("--impl", choices=("py", "rust"), default="py", help="hot-path stages 1-4")
    parser.add_argument("--json", dest="json_path", default=None, help="write results as JSON here")
    args = parser.parse_args(argv)
    if args.n < 1:
        parser.error("--n must be at least 1")

    backend = PyBackend if args.impl == "py" else rust_backend()
    messages = generate(args.n)
    pauses, mark = [], [0]

    def _cb(phase, _info):
        if phase == "start":
            mark[0] = NOW()
        elif mark[0]:
            pauses.append(NOW() - mark[0])
            mark[0] = 0

    gc.collect()
    if args.gc == "freeze":
        gc.freeze()
        gc.disable()
    gc.callbacks.append(_cb)
    started = NOW()
    try:
        samples, counters = run_ticks(messages, backend)
        samples.update(run_order_stages(args.n))
    finally:
        gc.callbacks.remove(_cb)
        if args.gc == "freeze":
            gc.unfreeze()
            gc.enable()
    wall_s = (NOW() - started) / 1e9

    summary = summarize(samples)
    ordered = sorted(pauses)
    gc_report = {"mode": args.gc, "collections": len(ordered), "p50_us": _pct(ordered, 50),
                 "p99_us": _pct(ordered, 99), "max_us": _pct(ordered, 100),
                 "total_ms": round(sum(ordered) / 1e6, 3)}
    print(f"impl={backend.name} gc={args.gc} n={args.n} wall_s={wall_s:.2f} "
          f"window={WINDOW_MS:g}ms sniper_jump_usd={JUMP_USD:g}")
    print(f"messages={counters['messages']} parsed={counters['parsed']} "
          f"triggers={counters['triggers']} prepared={counters['prepared']}")
    print(f"gc collections={gc_report['collections']} p50={_fmt(gc_report['p50_us'])}us "
          f"p99={_fmt(gc_report['p99_us'])}us max={_fmt(gc_report['max_us'])}us "
          f"total={gc_report['total_ms']}ms")
    print_table(summary)
    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"impl": backend.name, "gc": args.gc, "n": args.n,
                                    "wall_s": round(wall_s, 3),
                                    "config": {"window_ms": WINDOW_MS, "sniper_jump_usd": JUMP_USD},
                                    "counters": counters, "stages": summary,
                                    "gc_pauses": gc_report}, indent=2), encoding="utf-8")
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
