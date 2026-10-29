"""Sniper gate diagnostics: first-reject tracking, reject rate limit, POST TTFB."""
import json
import time
from types import SimpleNamespace

import buy.presign as presign
from buy.binance_trades import PriceGrid
from buy.fivemin import SNIPER_NOTE_ENTRIES, FiveMinIO, Strategy, order_latency_fields
from buy.hotpath import TickEffects
from buy.presign import PreSignedOrderCache, fast_post_order

import test_fivemin as helpers

SIDE_REJECT = "sniper_gate_reject"


def _grid(recv_wall_ms=1_200_020.0):
    grid = PriceGrid()
    grid.add(1_199_600.0, 70000.0, recv_ms=1_199_600.0)
    grid.add(1_200_000.0, 70030.0, recv_ms=1_200_000.0, recv_wall_ms=recv_wall_ms)
    return grid


def _case(result=None):
    case = SimpleNamespace(logs=[])
    case.clock = [1200.0]
    case.result = result or {"shares": 10, "cost": 9.4, "price": 0.94,
                             "status": "matched", "timing": {"post": 12.5}}
    case.io = FiveMinIO(
        buy=lambda *a, **kw: dict(case.result),
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


def _cfg():
    return helpers.cfg(fivemin_dry_run=False, sniper_5m_enabled=True, sniper_use_offset=False)


def _quotes(up_age_ms=12.0):
    return {"up": {"bid": .90, "bids": [], "asks": [[.94, 25.0]], "age_ms": up_age_ms, "src": "ws"},
            "down": {"bid": .06, "bids": [], "asks": [], "age_ms": 12.0, "src": "ws"}}


def _check(case, *, now=1200.0, quotes=None, grid=None, ttm=100.0, fire_path="poll"):
    return case.strategy._sniper_check(
        _cfg(), now, case.strategy.state["markets"]["cid"], quotes or _quotes(),
        grid or _grid(), ttm, defer_buy=True, fire_path=fire_path,
    )


def _events(case, name):
    return [kw for event, kw in case.logs if event == name]


def _latency_meta():
    return {"decision_ts": 1_700_000_000.000, "send_start_ts": 1_700_000_000.003,
            "resp_first_byte_ts": 1_700_000_000.040, "resp_complete_ts": 1_700_000_000.050}


def test_gate_reject_then_later_tick_fire_records_first_reject_and_path():
    case = _case()
    # First qualifying tick: the book is stale, so the up side is rejected.
    assert _check(case, quotes=_quotes(up_age_ms=5000.0), fire_path="tick") is None
    rejects = _events(case, SIDE_REJECT)
    assert len(rejects) == 1
    assert rejects[0]["side"] == "up"
    assert rejects[0]["gate"] == "book_age"
    assert rejects[0]["detail"]["age_ms"] == 5000.0
    assert rejects[0]["first_seen_ts"] == 1_200_020.0 / 1000.0
    json.dumps(rejects[0])

    # A later tick re-evaluates the same window with a fresh book and fires.
    # The window has slid by 300ms, but a fresh $30 jump is now in it, so the
    # later tick fires and credits its own receive stamp.
    later = PriceGrid()
    later.add(1_200_100.0, 70000.0, recv_ms=1_200_100.0)
    later.add(1_200_300.0, 70030.0, recv_ms=1_200_300.0, recv_wall_ms=1_200_450.0)
    prepared = _check(case, now=1200.3, grid=later, fire_path="tick")
    assert prepared is not None
    assert prepared["fire_path"] == "tick"
    assert prepared["first_seen_ts"] == 1_200_020.0 / 1000.0
    assert prepared["first_reject_gate"] == "book_age"
    assert prepared["first_reject_detail"] == {"age_ms": 5000.0, "max_age_ms": 1500.0}
    assert prepared["trigger_recv_ts"] == 1_200_450.0 / 1000.0

    prepared["post_meta"].update(_latency_meta())
    case.strategy._sniper_execute(prepared)
    fields = _events(case, "sniper_order_latency")[0]
    assert fields["fire_path"] == "tick"
    assert fields["fire_event_source"] == "primary"
    assert fields["fire_event_recv_ts"] == 1_200_450.0 / 1000.0
    assert fields["first_reject_gate"] == "book_age"
    assert fields["first_seen_ts"] == 1_200_020.0 / 1000.0
    # The old tick's receive stamp is credited here, exactly the case under test.
    assert fields["recv_to_decision_ms"] == round((1_700_000_000.0 - 1200.45) * 1000, 3)
    assert fields["first_seen_to_decision_ms"] == round((1_700_000_000.0 - 1200.02) * 1000, 3)
    assert fields["fire_event_to_decision_ms"] == round((1_700_000_000.0 - 1200.45) * 1000, 3)
    json.dumps(fields)


def test_poll_fire_path_is_reported_when_the_main_loop_fires():
    case = _case()
    prepared = _check(case)  # default fire_path is the main-loop poll
    assert prepared["fire_path"] == "poll"
    case.strategy._sniper_execute(prepared)
    assert _events(case, "sniper_order_latency")[0]["fire_path"] == "poll"


def test_gate_reject_is_rate_limited_per_gate_per_slug_and_side():
    case = _case()
    for _ in range(4):
        assert _check(case, quotes=_quotes(up_age_ms=5000.0)) is None
    rejects = _events(case, SIDE_REJECT)
    assert [(r["side"], r["gate"]) for r in rejects] == [("up", "book_age")]

    # A different gate on the same slug/side logs once more.
    below_range = _quotes()
    below_range["up"]["asks"] = [[.40, 25.0]]
    assert _check(case, quotes=below_range) is None
    assert [(r["side"], r["gate"]) for r in _events(case, SIDE_REJECT)] == [
        ("up", "book_age"), ("up", "ask_range")]

    # The down side has its own budget, but its jump is below threshold here.
    assert _events(case, SIDE_REJECT)[-1]["side"] == "up"


def test_gate_reject_covers_ttm_min_gap_and_min_fill():
    case = _case()
    assert _check(case, ttm=5.0) is None
    assert _check(case, ttm=900.0) is None
    ttm_gates = [r["gate"] for r in _events(case, SIDE_REJECT)]
    assert ttm_gates == ["ttm"]

    # Jump qualifies but the gap to strike is only $10 < sniper_min_gap_usd.
    grid = PriceGrid()
    grid.add(1_199_600.0, 69980.0, recv_ms=1_199_600.0)
    grid.add(1_200_000.0, 70010.0, recv_ms=1_200_000.0, recv_wall_ms=1_200_020.0)
    assert _check(case, grid=grid) is None
    gap = _events(case, SIDE_REJECT)[-1]
    assert (gap["side"], gap["gate"]) == ("up", "min_gap")
    assert gap["detail"]["gap_usd"] == 10.0

    # Ask below sniper_min_ask is both ask_range and the min-fill floor.
    cheap = _quotes()
    cheap["up"]["asks"] = [[.40, 25.0]]
    assert _check(case, quotes=cheap) is None
    assert _events(case, SIDE_REJECT)[-1]["gate"] == "ask_range"


def test_gate_reject_uses_the_deferred_log_path():
    case = _case()
    effects = TickEffects(case.io)
    original = case.io.log
    case.io.log = effects.log
    try:
        assert _check(case, quotes=_quotes(up_age_ms=5000.0)) is None
    finally:
        case.io.log = original
    # Buffered until the tick finishes, so nothing blocks before the POST.
    assert _events(case, SIDE_REJECT) == []
    assert len(effects) == 1
    effects.flush()
    assert _events(case, SIDE_REJECT)[0]["gate"] == "book_age"


def test_sniper_notes_are_bounded_per_slug():
    case = _case()
    for start in range(1000, 1000 + SNIPER_NOTE_ENTRIES * 4):
        row = {"slug": f"btc-up-or-down-5m-{start}", "end": 1300.0}
        case.strategy._sniper_note(row, "up")
    assert len(case.strategy._sniper_notes) == SNIPER_NOTE_ENTRIES


class _StubRequest:
    def __init__(self, url, content):
        self.url, self.content = url, content


class _StubHeaders(dict):
    def get(self, key, default=None):
        for name, value in self.items():
            if name.lower() == str(key).lower():
                return value
        return default


class _StubStream:
    def __init__(self, addr):
        self._addr = addr

    def get_extra_info(self, info):
        return self._addr if info == "client_addr" else None


class _StubResponse:
    def __init__(self, addr, body_delay=0.0):
        self.status_code = 200
        self.headers = _StubHeaders({
            "date": "Thu, 14 Nov 2024 22:13:20 GMT",
            "server-timing": "cfRequestDuration;dur=12.3",
            "x-response-time": "12ms",
            "cf-ray": "8c1f2a3b4c5d6e7f-AMS",
            "x-request-id": "req-abc-123",
            "cf-cache-status": "DYNAMIC",
        })
        self.extensions = {"http_version": b"HTTP/2", "network_stream": _StubStream(addr)}
        self.body_delay = body_delay
        self.content = json.dumps({"status": "matched", "timestamp": 1_700_000_000_123}).encode()

    def read(self):
        time.sleep(self.body_delay)
        return self.content

    def json(self):
        return json.loads(self.content)


class _StreamingHttp:
    """httpx-shaped client: ``send(stream=True)`` returns at the response headers."""

    def __init__(self, *, header_delay=0.02, body_delay=0.03, addr=("10.0.0.5", 40000)):
        self.header_delay, self.body_delay, self.addr = header_delay, body_delay, addr
        self.requests = []
        self.streamed = []

    def post(self, *_a, **_k):
        raise AssertionError("the streaming path must not fall back to a blocking post")

    def build_request(self, method, url, headers=None, content=None):
        self.requests.append((method, url, dict(headers or {}), content))
        return _StubRequest(url, content)

    def send(self, request, stream=False):
        assert stream is True
        self.streamed.append(True)
        time.sleep(self.header_delay)
        return _StubResponse(self.addr, body_delay=self.body_delay)


def _clob_client():
    import base64

    secret = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()

    class ClobClient:
        host = "https://clob.polymarket.com"
        creds = SimpleNamespace(api_key="k", api_secret=secret, api_passphrase="p")
        signer = SimpleNamespace(address=lambda: "0xabc")

    return ClobClient()


def test_fast_post_order_times_the_response_headers_not_the_send():
    presign._LAST_CONN["ident"] = None
    client = _clob_client()
    cache = PreSignedOrderCache()
    entry = cache.put(client, "tok", "BUY", 9.4, 0.94, {"salt": 7}, owner="0xabc")
    http = _StreamingHttp(header_delay=0.02, body_delay=0.03)
    meta = {}
    assert fast_post_order(client, entry.signed_order, entry=entry,
                           http_client=http, post_meta=meta)["status"] == "matched"

    assert http.streamed == [True]
    assert http.requests[0][3] == entry.serialized_bytes
    assert meta["resp_first_byte_ts"] - meta["send_start_ts"] >= 0.015
    assert meta["resp_complete_ts"] - meta["resp_first_byte_ts"] >= 0.025
    assert meta["http_status"] == 200
    assert meta["server_date"] == "Thu, 14 Nov 2024 22:13:20 GMT"
    assert meta["server_timing"] == "cfRequestDuration;dur=12.3"
    assert meta["x_response_time"] == "12ms"
    assert meta["cf_ray"] == "8c1f2a3b4c5d6e7f-AMS"
    assert meta["x_request_id"] == "req-abc-123"
    assert meta["cf_cache_status"] == "DYNAMIC"
    assert meta["http_version"] == "HTTP/2"
    assert meta["conn_reused"] is False
    assert meta["server_body_ts"] == 1_700_000_000_123


def test_connection_reuse_is_detected_across_posts():
    presign._LAST_CONN["ident"] = None
    client = _clob_client()
    cache = PreSignedOrderCache()
    http = _StreamingHttp(header_delay=0.0, body_delay=0.0)
    reused = []
    for salt in (1, 2):
        entry = cache.put(client, "tok", "BUY", 9.4, 0.94, {"salt": salt}, owner="0xabc")
        meta = {}
        fast_post_order(client, entry.signed_order, entry=entry, http_client=http, post_meta=meta)
        reused.append(meta["conn_reused"])
    assert reused == [False, True]


def test_plain_post_client_keeps_working_without_streaming():
    presign._LAST_CONN["ident"] = None
    client = _clob_client()
    cache = PreSignedOrderCache()
    entry = cache.put(client, "tok", "BUY", 9.4, 0.94, {"salt": 9}, owner="0xabc")

    class EchoResponse:
        status_code = 200
        headers = {"date": "Thu, 14 Nov 2024 22:13:20 GMT"}
        content = b'{"status":"matched"}'
        elapsed = SimpleNamespace(total_seconds=lambda: 0.02)

        def json(self):
            return {"status": "matched"}

    http = SimpleNamespace(post=lambda *_a, **_k: EchoResponse())
    meta = {}
    assert fast_post_order(client, entry.signed_order, entry=entry,
                           http_client=http, post_meta=meta)["status"] == "matched"
    assert meta["resp_first_byte_ts"] == meta["resp_complete_ts"] - 0.02
    assert meta["http_version"] is None
    assert meta["server_timing"] is None


def test_latency_fields_surface_the_transport_and_fire_diagnostics():
    prepared = {"row": {"slug": "btc-up-or-down-5m-1000"}, "quotes": {}, "fav": "up",
                "limit": 0.97, "trigger_source": "deribit", "is_dry": False,
                "fire_path": "tick", "fire_event_source": "deribit",
                "fire_event_recv_ts": 1_700_000_000.0,
                "first_seen_ts": 1_699_999_999.7,
                "first_reject_gate": "book_age",
                "first_reject_detail": {"age_ms": 5000.0},
                "post_meta": dict(_latency_meta(), cf_ray="abc-AMS", http_version="HTTP/2",
                                  conn_reused=True, server_timing="cfRequestDuration;dur=9",
                                  x_response_time="9ms", x_request_id="rid",
                                  cf_cache_status="DYNAMIC")}
    fields = order_latency_fields(prepared, {"shares": 0, "status": "unfilled"})
    assert fields["fire_path"] == "tick"
    assert fields["fire_event_source"] == "deribit"
    assert fields["first_reject_gate"] == "book_age"
    assert fields["first_reject_detail"] == {"age_ms": 5000.0}
    assert fields["first_seen_to_decision_ms"] == 300.0
    assert fields["fire_event_to_decision_ms"] == 0.0
    assert fields["ttfb_ms"] == 37.0
    assert fields["body_ms"] == 10.0
    assert fields["conn_reused"] is True
    assert fields["http_version"] == "HTTP/2"
    assert fields["server_timing"] == "cfRequestDuration;dur=9"
    assert fields["x_response_time"] == "9ms"
    assert fields["cf_ray"] == "abc-AMS"
    assert fields["x_request_id"] == "rid"
    assert fields["cf_cache_status"] == "DYNAMIC"
    json.dumps(fields)


import unittest as _unittest


class SniperGateDiagTest(_unittest.TestCase):
    """Runs every module-level ``test_*`` function under ``unittest``."""


for _name, _fn in list(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        setattr(SniperGateDiagTest, _name, (lambda f: lambda self: f())(_fn))
del _name, _fn
