"""Per-order sniper latency: one structured JSON line for fill, nofill and error."""
import ast
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from buy.binance_trades import PriceGrid
from buy.fivemin import FiveMinIO, Strategy, order_latency_fields
from buy.presign import PreSignedOrderCache, fast_post_order

import test_fivemin as helpers


def _grid(recv_wall_ms=1_200_020.0):
    grid = PriceGrid()
    grid.add(1_199_600.0, 70000.0, recv_ms=1_199_600.0)
    grid.add(1_200_000.0, 70030.0, recv_ms=1_200_000.0, recv_wall_ms=recv_wall_ms)
    return grid


REQUIRED = {
    "slug", "side", "feed", "trigger_exchange_ts", "trigger_recv_ts", "decision_ts",
    "sign_done_ts", "presign_hit", "send_start_ts", "resp_first_byte_ts",
    "resp_complete_ts", "server_date", "server_body_ts", "http_status",
    "recv_to_decision_ms", "decision_to_send_ms", "send_to_complete_ms",
    "trigger_exchange_to_complete_ms", "ask_seen", "limit", "fill_price",
    "filled_shares", "user_ws_match_ts",
}


def _case(result=None, buy=None):
    case = SimpleNamespace(logs=[])
    case.clock = [1200.0]
    case.result = result or {"shares": 10, "cost": 9.4, "price": 0.94,
                             "status": "matched", "timing": {"post": 12.5}}
    case.io = FiveMinIO(
        buy=buy or (lambda *a, **kw: dict(case.result)),
        sell=lambda *a: {}, reserves=lambda cfg: 0, redeem=lambda *a: None,
        save=lambda state: None,
        log=lambda event, **kw: case.logs.append((event, kw)),
        alert=lambda *a, **kw: None, clock=lambda: case.clock[0],
    )
    case.strategy = Strategy(case.io, startup_ts=0)
    case.strategy.add_market(helpers.market(), now=1000)
    row = case.strategy.state["markets"]["cid"]
    row["strike"] = 70000.0
    row["offset"] = 0.0
    case.strategy.sample_cash(1200, 1000)
    return case


def _quotes():
    return {"up": {"bid": .90, "bids": [], "asks": [[.94, 25.0]], "age_ms": 12.0, "src": "ws"},
            "down": {"bid": .06, "bids": [], "asks": [], "age_ms": 12.0, "src": "ws"}}


def _run(case, *, post_meta=None):
    quotes = _quotes()
    prepared = case.strategy._sniper_check(
        helpers.cfg(fivemin_dry_run=False, sniper_5m_enabled=True, sniper_use_offset=False), 1200.0,
        case.strategy.state["markets"]["cid"], quotes, _grid(), 100.0, defer_buy=True,
    )
    assert prepared is not None
    if post_meta is not None:
        prepared["post_meta"].update(post_meta)
    case.strategy._sniper_execute(prepared)
    return [kw for event, kw in case.logs if event == "sniper_order_latency"]


def _fixed_meta():
    return {"decision_ts": 1_700_000_000.000, "sign_done_ts": 1_700_000_000.002,
            "presign_hit": True, "send_start_ts": 1_700_000_000.003,
            "resp_first_byte_ts": 1_700_000_000.040, "resp_complete_ts": 1_700_000_000.050,
            "server_date": "Thu, 14 Nov 2024 22:13:20 GMT", "server_body_ts": 1_700_000_000_048,
            "http_status": 200}


def test_fill_emits_one_latency_line_with_all_fields_and_deltas():
    case = _case()
    prepared = case.strategy._sniper_check(
        helpers.cfg(fivemin_dry_run=False, sniper_5m_enabled=True, sniper_use_offset=False), 1200.0,
        case.strategy.state["markets"]["cid"], _quotes(), _grid(recv_wall_ms=1_200_015.0),
        100.0, defer_buy=True,
    )
    # Trigger timestamps are captured from the feed: exchange ms -> epoch s, local recv -> epoch s.
    assert prepared["trigger_source"] == "primary"
    assert prepared["trigger_exchange_ts"] == 1200.0
    assert prepared["trigger_recv_ts"] == 1200.015

    prepared["post_meta"].update(_fixed_meta())
    case.strategy._sniper_execute(prepared)
    lines = [kw for event, kw in case.logs if event == "sniper_order_latency"]
    assert len(lines) == 1
    fields = lines[0]
    assert REQUIRED <= set(fields)
    json.dumps(fields)  # one JSON-serializable line
    assert fields["slug"] == "btc-up-or-down-5m-1000"
    assert fields["side"] == "up"
    assert round(fields["ask_seen"], 4) == .94
    assert fields["limit"] == 0.97
    assert round(fields["fill_price"], 4) == 0.94
    assert fields["filled_shares"] == 10.0
    assert fields["user_ws_match_ts"] is None
    assert fields["http_status"] == 200
    assert fields["server_body_ts"] == 1_700_000_000_048
    assert fields["transport_ms"] == 12.5
    assert fields["recv_to_decision_ms"] == round((1_700_000_000.0 - 1200.015) * 1000, 3)
    assert fields["trigger_exchange_to_decision_ms"] == round((1_700_000_000.0 - 1200.0) * 1000, 3)
    assert fields["decision_to_send_ms"] == 3.0
    assert fields["send_to_complete_ms"] == 47.0
    assert fields["trigger_exchange_to_complete_ms"] == round((1_700_000_000.050 - 1200.0) * 1000, 3)


def test_nofill_still_logs_a_line():
    case = _case(result={"shares": 0, "cost": 0, "status": "unfilled", "timing": {"post": 9.0}})
    fields = _run(case, post_meta=_fixed_meta())[0]
    assert fields["filled_shares"] == 0.0
    assert fields["fill_price"] is None
    assert fields["status"] == "unfilled"
    assert fields["send_to_complete_ms"] == 47.0


def test_error_logs_a_line_with_error_status():
    def boom(*_a, **_k):
        raise RuntimeError("socket closed")

    case = _case(buy=boom)
    lines = _run(case, post_meta={"decision_ts": 1_700_000_000.0})
    assert len(lines) == 1
    fields = lines[0]
    assert REQUIRED <= set(fields)
    assert fields["status"] == "error:socket closed"
    assert fields["filled_shares"] == 0.0
    assert fields["resp_complete_ts"] is not None


def test_feed_recv_and_first_feed_in_latency_line():
    case = _case()
    grid = _grid(recv_wall_ms=1_200_020.0)
    # Futures feed tick arrived 6ms before spot: both inside the jump window.
    grid.add(1_200_000.0, 70030.0, source="binance_fut",
             recv_ms=1_199_994.0, recv_wall_ms=1_200_014.0)
    prepared = case.strategy._sniper_check(
        helpers.cfg(fivemin_dry_run=False, sniper_5m_enabled=True, sniper_use_offset=False), 1200.0,
        case.strategy.state["markets"]["cid"], _quotes(), grid, 100.0, defer_buy=True,
    )
    assert prepared is not None
    case.strategy._sniper_execute(prepared)
    fields = [kw for event, kw in case.logs if event == "sniper_order_latency"][0]
    assert fields["feed_recv"] == {"primary": 1_200_020.0, "binance_fut": 1_200_014.0}
    assert fields["first_feed"] == "binance_fut"
    json.dumps(fields)


def test_feed_recv_excludes_feeds_outside_the_jump_window():
    case = _case()
    grid = _grid(recv_wall_ms=1_200_020.0)
    grid.add(1_200_000.0, 70030.0, source="deribit",
             recv_ms=1_198_000.0, recv_wall_ms=1_198_000.0)
    prepared = case.strategy._sniper_check(
        helpers.cfg(fivemin_dry_run=False, sniper_5m_enabled=True, sniper_use_offset=False), 1200.0,
        case.strategy.state["markets"]["cid"], _quotes(), grid, 100.0, defer_buy=True,
    )
    case.strategy._sniper_execute(prepared)
    fields = [kw for event, kw in case.logs if event == "sniper_order_latency"][0]
    assert fields["feed_recv"] == {"primary": 1_200_020.0}
    assert fields["first_feed"] == "primary"


def test_latency_fields_accept_explicit_feed_snapshot():
    prepared = {"row": {"slug": "s"}, "quotes": {}, "fav": "up", "limit": 0.97,
                "trigger_source": "binance_fut", "is_dry": False,
                "post_meta": {"decision_ts": 10.0}}
    fields = order_latency_fields(prepared, {"shares": 0, "status": "unfilled"},
                                  feed_recv={"binance_fut": 500.0}, first_feed="binance_fut")
    assert fields["feed_recv"] == {"binance_fut": 500.0}
    assert fields["first_feed"] == "binance_fut"
    assert fields["feed"] == "binance_fut"


def test_missing_timestamps_yield_null_deltas():
    prepared = {"row": {"slug": "s"}, "quotes": {}, "fav": "up", "limit": 0.97,
                "trigger_source": "primary", "is_dry": False,
                "trigger_exchange_ts": None, "trigger_recv_ts": None,
                "post_meta": {"decision_ts": 10.0}}
    fields = order_latency_fields(prepared, {"shares": 0, "status": "unfilled"})
    assert fields["decision_ts"] == 10.0
    assert fields["send_start_ts"] is None
    assert fields["recv_to_decision_ms"] is None
    assert fields["trigger_exchange_to_complete_ms"] is None


def test_fast_post_order_captures_meta_after_the_http2_post():
    entry_cache = PreSignedOrderCache()
    raw = {"status": "matched", "timestamp": 1_700_000_000_123}

    class Resp:
        status_code = 200
        headers = {"date": "Thu, 14 Nov 2024 22:13:20 GMT"}
        content = json.dumps(raw).encode()
        elapsed = SimpleNamespace(total_seconds=lambda: 0.02)

        def json(self):
            return raw

    class ClobClient:
        host = "https://clob.polymarket.com"
        creds = SimpleNamespace(api_key="k", api_secret="c2VjcmV0", api_passphrase="p")
        signer = SimpleNamespace(address=lambda: "0xabc")

    client = ClobClient()
    entry = entry_cache.put(client, "tok", "BUY", 9.4, 0.94, {"salt": 7}, owner="0xabc")
    http = SimpleNamespace(post=lambda *_a, **_k: Resp())
    meta = {}
    assert fast_post_order(client, entry.signed_order, entry=entry,
                           http_client=http, post_meta=meta)["status"] == "matched"
    assert meta["send_start_ts"] <= meta["resp_complete_ts"]
    assert meta["http_status"] == 200
    assert meta["server_date"] == "Thu, 14 Nov 2024 22:13:20 GMT"
    assert meta["server_body_ts"] == 1_700_000_000_123
    assert meta["resp_first_byte_ts"] == meta["resp_complete_ts"] - 0.02


def test_fivemin_buy_forwards_post_meta_into_fak_buy():
    # AGENTS.md: never import mintbot in tests; extract the one function.
    mint = Path(__file__).resolve().parents[1] / "mintbot.py"
    tree = ast.parse(mint.read_text(), filename=str(mint))
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_fivemin_buy")
    seen = {}

    def fake_fak(token, shares, limit, dry, capture=None, *, timing=None, post_meta=None):
        seen["post_meta"] = post_meta
        return 0.0, "unfilled"

    ns = {"_fak_buy": fake_fak, "math": math, "Optional": Optional,
          "buy_fill_vwap": lambda _raw, _bought: None}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(mint), "exec"), ns)
    meta = {}
    ns["_fivemin_buy"]("tok", 10.0, 0.97, False, post_meta=meta)
    assert seen["post_meta"] is meta
