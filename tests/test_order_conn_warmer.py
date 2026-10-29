"""Order connection warmer: pings, in-flight skips, reconnect on error."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from buy import presign


class FakeResp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class FakeClient:
    def __init__(self, fail: bool = False, status_code: int = 200) -> None:
        self.fail = fail
        self.status_code = status_code
        self.gets: list[tuple[str, float | None]] = []
        self.posts: list[tuple[str, dict, bytes]] = []
        self.closed = 0

    def get(self, url, timeout=None):
        self.gets.append((url, timeout))
        if self.fail:
            raise RuntimeError("connection reset")
        return FakeResp(self.status_code)

    def post(self, url, headers=None, content=None):
        self.posts.append((url, headers or {}, content or b""))
        return FakeResp(200)

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def _reset_state():
    presign.stop_order_conn_warmer()
    presign.set_order_http_client(None, factory=None)
    presign._ORDER_CLIENT_FACTORY = None
    presign._LAST_ORDER_ACTIVITY_TS = 0.0
    presign._ORDER_INFLIGHT = 0
    for key in presign._ORDER_WARM_STATS:
        presign._ORDER_WARM_STATS[key] = 0 if key != "last_rtt_ms" else None
    yield
    presign.stop_order_conn_warmer()
    presign.set_order_http_client(None, factory=None)
    presign._ORDER_CLIENT_FACTORY = None
    presign._LAST_ORDER_ACTIVITY_TS = 0.0
    presign._ORDER_INFLIGHT = 0


def test_tick_pings_the_same_order_client():
    client = FakeClient()
    presign.set_order_http_client(client)

    assert presign.order_warm_tick("https://clob.example/", 2.0) == "ok"

    assert client.gets == [("https://clob.example/time", presign.ORDER_WARM_TIMEOUT_S)]
    stats = presign.order_conn_stats()
    assert stats["pings_ok"] == 1
    assert stats["pings_fail"] == 0
    assert stats["reconnects"] == 0
    assert isinstance(stats["last_rtt_ms"], float)


def test_tick_skips_while_an_order_is_in_flight():
    client = FakeClient()
    presign.set_order_http_client(client)
    presign._ORDER_INFLIGHT = 1
    try:
        assert presign.order_warm_tick("https://clob.example", 2.0) == "inflight"
    finally:
        presign._ORDER_INFLIGHT = 0
    assert client.gets == []
    assert presign.order_conn_stats()["skips_inflight"] == 1


def test_tick_skips_when_an_order_was_just_sent():
    client = FakeClient()
    presign.set_order_http_client(client)
    presign._LAST_ORDER_ACTIVITY_TS = time.time()

    assert presign.order_warm_tick("https://clob.example", 60.0) == "recent"

    assert client.gets == []
    assert presign.order_conn_stats()["skips_recent"] == 1


def test_ping_error_swaps_in_a_fresh_client_and_delays_the_close():
    old = FakeClient(fail=True)
    new = FakeClient()
    made = []

    def factory():
        made.append(FakeClient())
        return made[-1]

    presign.set_order_http_client(old, factory=factory)
    assert presign.order_warm_tick("https://clob.example", 2.0, close_delay_s=0.05) == "fail"

    assert presign.get_order_http_client() is made[0]
    assert presign.order_conn_stats()["pings_fail"] == 1
    assert presign.order_conn_stats()["reconnects"] == 1
    # The old client is closed in the background, not on the tick.
    deadline = time.time() + 2.0
    while old.closed == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert old.closed == 1

    # The next tick pings the new client.
    assert presign.order_warm_tick("https://clob.example", 2.0) == "ok"
    assert len(made[0].gets) == 1
    assert presign.order_conn_stats()["pings_ok"] == 1


def test_non_200_response_counts_as_failure_and_reconnects():
    old = FakeClient(status_code=503)
    new = FakeClient()
    presign.set_order_http_client(old, factory=lambda: new)

    assert presign.order_warm_tick("https://clob.example", 2.0, close_delay_s=0.0) == "fail"

    assert presign.get_order_http_client() is new
    assert presign.order_conn_stats()["pings_fail"] == 1
    assert presign.order_conn_stats()["reconnects"] == 1


def test_factory_failure_keeps_the_current_client():
    old = FakeClient(fail=True)

    def factory():
        raise RuntimeError("no dns")

    presign.set_order_http_client(old, factory=factory)

    assert presign.order_warm_tick("https://clob.example", 2.0) == "fail"
    assert presign.get_order_http_client() is old
    assert presign.order_conn_stats()["reconnects"] == 0
    assert presign.order_conn_stats()["reconnect_fail"] == 1


def test_fast_post_order_marks_the_post_in_flight():
    import base64

    raw_secret = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    seen = []

    class Hc:
        def post(self, url, headers=None, content=None):
            seen.append(presign.order_inflight())
            return SimpleNamespace(
                status_code=200,
                content=b'{"status":"matched"}',
                json=lambda: {"status": "matched"},
            )

    class ClobClient:
        host = "https://clob.polymarket.com"
        creds = SimpleNamespace(api_key="k1", api_secret=raw_secret, api_passphrase="p1")
        signer = SimpleNamespace(address=lambda: "0xsigner")

    clob = ClobClient()
    cache = presign.PreSignedOrderCache()
    entry = cache.put(clob, "tok1", "BUY", 4.85, 0.97, {"salt": 1}, owner="k1")
    presign.set_order_http_client(Hc())

    res = presign.fast_post_order(clob, entry.signed_order, entry=entry, order_type="FAK")

    assert res["status"] == "matched"
    assert seen == [1]
    assert presign.order_inflight() == 0


def test_warmer_thread_pings_and_logs_a_summary():
    client = FakeClient()
    presign.set_order_http_client(client)
    events = []
    stop = threading.Event()

    def log(event, **fields):
        events.append((event, fields))
        stop.set()

    thread = presign.start_order_conn_warmer(
        "https://clob.example",
        0.05,
        factory=lambda: FakeClient(),
        log=log,
        summary_s=0.0,
    )
    try:
        assert thread is not None and thread.daemon
        assert thread.name == "order-conn-warmer"
        assert stop.wait(2.0), "warmer did not log a summary"
    finally:
        presign.stop_order_conn_warmer()

    assert len(client.gets) >= 1
    event, fields = events[0]
    assert event == "order_conn_warm"
    assert "pings_ok" in fields and "reconnects" in fields and "last_rtt_ms" in fields
    assert fields["interval_s"] == 0.05


def test_warmer_start_is_idempotent():
    presign.set_order_http_client(FakeClient())
    first = presign.start_order_conn_warmer("https://clob.example", 0.5, factory=FakeClient)
    try:
        second = presign.start_order_conn_warmer("https://clob.example", 0.5, factory=FakeClient)
        assert first is second
    finally:
        presign.stop_order_conn_warmer()
