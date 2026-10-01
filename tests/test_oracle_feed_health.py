"""Oracle tape hardening: feed watchdog, stall logging, crypto-price / Gamma retries."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from buy import oracle_log
from buy.oracle_log import (
    CLOSE_GRACE_S,
    FEED_PING_INTERVAL_S,
    FEED_PING_TIMEOUT_S,
    FEED_SILENT_RECONNECT_S,
    GAMMA_FIRST_S,
    GAMMA_GIVE_UP_S,
    GAMMA_RETRY_S,
    GRACE_AFTER_S,
    HTTP_RETRY_S,
    CryptoPriceHTTPError,
    GammaStrike,
    OracleLogService,
    RtdsTwapFeed,
    WindowPrice,
    classify_http_error,
    fetch_crypto_price,
    http_retry_delay_s,
    parse_retry_after,
)
from test_oracle_log import END, START, FakeFeed, _bag, _rows, _sample


class ReconnectFeed(FakeFeed):
    def __init__(self) -> None:
        super().__init__()
        self.reconnects: list[str] = []

    def reconnect(self, reason: str) -> bool:
        self.reconnects.append(reason)
        return True


def _no_gamma(slug: str) -> GammaStrike:
    del slug
    return GammaStrike(price_to_beat=None, final_price=None)


def _service(feed, fetch, **kw):
    folder = Path(tempfile.mkdtemp(prefix="oracle-health-"))
    path = folder / "oracle_twap.jsonl"
    kw.setdefault("fetch_gamma", _no_gamma)
    service = OracleLogService(path, feed=feed, fetch_price=fetch, jitter=lambda: 0.0, **kw)
    return service, path


def _open_only(start_ts: int) -> WindowPrice:
    del start_ts
    return WindowPrice(open_ref="100", close_twap=None, completed=False)


class Recorder:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.events: list[tuple[str, dict]] = []

    def kwargs(self) -> dict:
        return {"on_fail": self.fails.append, "on_event": self._event}

    def _event(self, name: str, fields: dict) -> None:
        self.events.append((name, dict(fields)))

    def named(self, name: str) -> list[dict]:
        return [fields for event, fields in self.events if event == name]


class WatchdogTests(unittest.TestCase):
    def test_silent_feed_is_reconnected_with_backoff(self):
        feed = ReconnectFeed()
        t0 = START + 100
        feed.latest_sample = _sample(t0)
        feed.samples = [feed.latest_sample]
        service, _path = _service(feed, _open_only)
        rec = Recorder()
        service.tick(_bag(), t0, enabled=True, **rec.kwargs())
        fired: list[int] = []
        for dt in range(1, 261):
            before = len(feed.reconnects)
            service.tick(_bag(), t0 + dt, enabled=True, **rec.kwargs())
            if len(feed.reconnects) > before:
                fired.append(dt)
        self.assertEqual(fired, [45, 135, 255])
        self.assertTrue(feed.reconnects[0].startswith("watchdog: silent 45s"))
        self.assertEqual(len(rec.named("oracle_feed_watchdog")), 3)

        # A sample resets the watchdog to the first 45s gap.
        feed.latest_sample = _sample(t0 + 261)
        feed.samples = [feed.latest_sample]
        service.tick(_bag(), t0 + 261, enabled=True, **rec.kwargs())
        fired.clear()
        for dt in range(262, 400):
            before = len(feed.reconnects)
            service.tick(_bag(), t0 + dt, enabled=True, **rec.kwargs())
            if len(feed.reconnects) > before:
                fired.append(dt)
        self.assertEqual(fired[0], 261 + int(FEED_SILENT_RECONNECT_S))

    def test_no_reconnect_without_a_tracked_bag(self):
        feed = ReconnectFeed()
        service, _path = _service(feed, _open_only)
        for dt in range(0, 200):
            service.tick({"intents": {}}, START + dt, enabled=True)
        self.assertEqual(feed.reconnects, [])
        self.assertEqual(feed.started, 0)

    def test_steady_feed_is_never_reconnected(self):
        feed = ReconnectFeed()
        service, _path = _service(feed, _open_only)
        for dt in range(0, 200):
            feed.latest_sample = _sample(START + 100 + dt)
            feed.samples = [feed.latest_sample]
            service.tick(_bag(), START + 100 + dt, enabled=True)
        self.assertEqual(feed.reconnects, [])

    def test_feed_reconnect_closes_socket_and_backs_off(self):
        feed = RtdsTwapFeed()
        self.assertFalse(feed.reconnect("no socket yet"))
        ws = mock.Mock()
        feed._ws = ws
        self.assertTrue(feed.reconnect("watchdog: silent 45s"))
        ws.close.assert_called_once()
        feed._set_error("ping/pong timed out")
        waits = []
        backoff = 1.0
        for _ in range(7):
            wait, backoff = feed._after_disconnect(backoff)
            waits.append(wait)
            feed._force_reason = ""
        self.assertEqual(waits, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0])
        events = feed.pop_events()
        self.assertEqual(len(events), 7)
        self.assertEqual(events[0]["reason"], "watchdog: silent 45s")
        self.assertEqual(events[0]["last_error"], "ping/pong timed out")
        self.assertEqual(events[1]["reason"], "no_samples")
        self.assertEqual(events[-1]["attempt"], 7)
        self.assertEqual(feed.pop_events(), [])

        frame = json.dumps(
            {
                "topic": "crypto_prices_twap_sixty",
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": 1_790_063_100_000,
                    "full_accuracy_value": "85260000000000000000000",
                },
            }
        )
        feed.handle_message(frame)
        wait, backoff = feed._after_disconnect(backoff)
        self.assertEqual((wait, backoff), (1.0, 2.0))
        self.assertEqual(feed.pop_events()[0]["attempt"], 0)

    def test_run_once_enables_protocol_ping_pong(self):
        seen = {}

        class FakeApp:
            def __init__(self, url, **callbacks):
                seen["url"] = url
                seen["callbacks"] = sorted(callbacks)

            def run_forever(self, **kwargs):
                seen["kwargs"] = kwargs

            def send(self, _data):
                return None

        feed = RtdsTwapFeed()
        feed._run_once(SimpleNamespace(WebSocketApp=FakeApp))
        self.assertEqual(
            seen["kwargs"],
            {"ping_interval": FEED_PING_INTERVAL_S, "ping_timeout": FEED_PING_TIMEOUT_S},
        )
        self.assertGreater(FEED_PING_INTERVAL_S, FEED_PING_TIMEOUT_S)

    def test_reconnect_events_are_logged_and_throttled(self):
        feed = ReconnectFeed()
        queue: list[dict] = []
        feed.pop_events = lambda: [queue.pop(0)] if queue else []
        feed.latest_sample = _sample(START + 100)
        service, _path = _service(feed, _open_only)
        rec = Recorder()
        for dt in range(0, 125):
            queue.append({"kind": "reconnect", "reason": "no_samples", "last_error": "boom", "attempt": dt, "backoff_s": 1.0})
            feed.latest_sample = _sample(START + 100 + dt)
            feed.samples = [feed.latest_sample]
            service.tick(_bag(), START + 100 + dt, enabled=True, **rec.kwargs())
        logged = rec.named("oracle_feed_reconnect")
        self.assertEqual(len(logged), 3)
        self.assertEqual(logged[0]["last_error"], "boom")
        self.assertEqual(logged[0]["suppressed"], 0)
        self.assertEqual(logged[1]["suppressed"], 59)


class StallLoggingTests(unittest.TestCase):
    def _run_stall(self, rec: Recorder, *, hook: bool = True):
        feed = ReconnectFeed()
        feed.error = "ping/pong timed out"
        t0 = START + 100
        feed.latest_sample = _sample(t0)
        feed.samples = [feed.latest_sample]
        service, path = _service(feed, _open_only)
        kwargs = rec.kwargs() if hook else {"on_fail": rec.fails.append}
        service.tick(_bag(), t0, enabled=True, **kwargs)
        for dt in range(1, 201):
            service.tick(_bag(), t0 + dt, enabled=True, **kwargs)
        feed.latest_sample = _sample(t0 + 201)
        feed.samples = [feed.latest_sample]
        service.tick(_bag(), t0 + 202, enabled=True, **kwargs)
        for dt in range(203, 260):
            feed.latest_sample = _sample(t0 + dt)
            feed.samples = [feed.latest_sample]
            service.tick(_bag(), t0 + dt, enabled=True, **kwargs)
        return path

    def test_stall_logs_start_reminders_and_recovery_only(self):
        rec = Recorder()
        path = self._run_stall(rec)
        stalls = rec.named("oracle_feed_stall")
        self.assertEqual([s["reminder"] for s in stalls], [0, 1, 2])
        self.assertEqual(stalls[0]["age_s"], 21.0)
        self.assertEqual(stalls[0]["last_error"], "ping/pong timed out")
        self.assertEqual(stalls[0]["slug"], f"btc-updown-15m-{int(START)}")
        recovered = rec.named("oracle_feed_recovered")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["duration_s"], 202.0)
        self.assertEqual(rec.fails, [])
        rows = _rows(path)
        self.assertFalse(any(row["event"] == "oracle_log_fail" for row in rows))
        self.assertEqual(sum(row["event"] == "oracle_feed_stall" for row in rows), 3)
        self.assertEqual(sum(row["event"] == "oracle_feed_recovered" for row in rows), 1)

    def test_without_event_hook_stall_is_still_not_per_second(self):
        rec = Recorder()
        self._run_stall(rec, hook=False)
        stall_lines = [f for f in rec.fails if f.startswith("oracle_feed_stall")]
        self.assertEqual(len(stall_lines), 3)
        self.assertEqual(sum(f.startswith("oracle_feed_recovered") for f in rec.fails), 1)
        self.assertLess(len(rec.fails), 12)

    def test_no_sample_yet_counts_as_a_stall(self):
        feed = ReconnectFeed()
        service, _path = _service(feed, _open_only)
        rec = Recorder()
        for dt in range(0, 30):
            service.tick(_bag(), START + 100 + dt, enabled=True, **rec.kwargs())
        stalls = rec.named("oracle_feed_stall")
        self.assertEqual(len(stalls), 1)
        self.assertTrue(stalls[0]["no_sample_yet"])

    def test_fail_dedupes_by_kind_not_text(self):
        feed = FakeFeed()
        service, _path = _service(feed, _open_only)
        fails: list[str] = []
        service._on_fail = fails.append
        for dt in range(0, 29):
            service._fail(f"feed: age={dt}s", now=START + dt, window=None, kind="feed")
        self.assertEqual(len(fails), 1)
        service._fail("feed: again", now=START + 30, window=None, kind="feed")
        service._fail("tick: other kind", now=START + 30, window=None, kind="tick")
        self.assertEqual(len(fails), 3)


class HttpRetryTests(unittest.TestCase):
    def test_retry_delay_schedule(self):
        self.assertEqual(
            [http_retry_delay_s("http_429", n, None) for n in range(1, 7)],
            [20.0, 40.0, 80.0, 120.0, 120.0, 120.0],
        )
        self.assertEqual(http_retry_delay_s("http_429", 1, 90.0), 90.0)
        self.assertEqual(http_retry_delay_s("http_429", 3, 5.0), 80.0)
        self.assertEqual(http_retry_delay_s("http_400", 0, None), HTTP_RETRY_S)
        self.assertEqual(http_retry_delay_s("error", 0, 99.0), HTTP_RETRY_S)

    def test_classify_and_retry_after(self):
        self.assertEqual(classify_http_error(CryptoPriceHTTPError(429, 7.0)), ("http_429", 7.0))
        self.assertEqual(classify_http_error(CryptoPriceHTTPError(400, 7.0)), ("http_400", None))
        requests_like = RuntimeError("429 Client Error")
        requests_like.response = SimpleNamespace(status_code=429, headers={"Retry-After": "12"})
        self.assertEqual(classify_http_error(requests_like), ("http_429", 12.0))
        self.assertEqual(classify_http_error(TimeoutError("slow")), ("error", None))
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("soon"))
        self.assertEqual(parse_retry_after("30"), 30.0)
        self.assertEqual(parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT"), 0.0)

    def test_fetch_raises_status_and_retry_after(self):
        response = SimpleNamespace(status_code=429, headers={"Retry-After": "5"}, text="slow down")
        session = SimpleNamespace(get=lambda *a, **k: response)
        with mock.patch.object(oracle_log, "thread_session", lambda _name: session):
            with self.assertRaises(CryptoPriceHTTPError) as caught:
                fetch_crypto_price(int(START))
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 5.0)
        ok = SimpleNamespace(
            status_code=200,
            headers={},
            text='{"openPrice": 100.5, "closePrice": null, "completed": false, "incomplete": true}',
        )
        session = SimpleNamespace(get=lambda *a, **k: ok)
        with mock.patch.object(oracle_log, "thread_session", lambda _name: session):
            price = fetch_crypto_price(int(START))
        self.assertEqual(price, WindowPrice(open_ref="100.5", close_twap=None, completed=False))

    def _checked_service(self, fetch_gamma, **memory_kw):
        feed = FakeFeed()
        service, path = _service(feed, _open_only, fetch_gamma=fetch_gamma)
        window = oracle_log.OracleWindow(
            condition_id="cid-15m", slug=f"btc-updown-15m-{int(START)}", start_ts=START, end_ts=END,
        )
        memory = oracle_log._WindowMemory(window=window, **memory_kw)
        service._memory["cid-15m"] = memory
        return service, path, memory

    def _run(self, service, clock, start, stop, rec=None):
        for now in range(int(start), int(stop)):
            clock["now"] = float(now)
            kwargs = rec.kwargs() if rec is not None else {}
            service.tick({"intents": {}}, float(now), enabled=True, **kwargs)

    def test_gamma_429_backs_off_and_gives_up_after_an_hour(self):
        calls: list[float] = []
        clock = {"now": 0.0}

        def gamma(slug: str) -> GammaStrike:
            self.assertEqual(slug, f"btc-updown-15m-{int(START)}")
            calls.append(clock["now"] - END)
            raise CryptoPriceHTTPError(429)

        service, path, _memory = self._checked_service(
            gamma, open_ref="100", open_source=oracle_log.STRIKE_RTDS,
        )
        rec = Recorder()
        self._run(service, clock, END, END + GAMMA_GIVE_UP_S + 5, rec)
        self.assertEqual(calls, [600.0, 720.0, 960.0, 1440.0, 2040.0, 2640.0, 3240.0])
        http_fails = [f for f in rec.fails if f.startswith("gamma")]
        self.assertEqual(len(http_fails), 1)
        self.assertIn("http_429", http_fails[0])
        missed = rec.named("oracle_check_missed")
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["missing"], ["price_to_beat", "final_price"])
        self.assertEqual(missed[0]["counts"], {"http_429": 7})
        self.assertEqual(missed[0]["attempts"], 7)
        self.assertEqual(missed[0]["strike_source"], "rtds_twap_at_start")
        self.assertFalse(any(row["event"] == "oracle_strike_check" for row in _rows(path)))
        self._run(service, clock, END + GAMMA_GIVE_UP_S + 5, END + GAMMA_GIVE_UP_S + 200, rec)
        self.assertEqual(len(calls), 7)
        self.assertEqual(service._memory, {})

    def test_gamma_is_never_polled_per_second(self):
        calls: list[float] = []
        clock = {"now": 0.0}

        def gamma(slug: str) -> GammaStrike:
            del slug
            calls.append(clock["now"] - END)
            return GammaStrike(price_to_beat=None, final_price=None)

        service, _path, memory = self._checked_service(gamma, open_ref="100")
        self._run(service, clock, START, END + GAMMA_GIVE_UP_S + 5)
        self.assertEqual(calls[0], 600.0)
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        self.assertTrue(gaps and min(gaps) >= GAMMA_RETRY_S)
        self.assertLessEqual(len(calls), int((GAMMA_GIVE_UP_S - 600) / GAMMA_RETRY_S) + 1)
        self.assertEqual(memory.check_http.counts, {"unpublished": len(calls)})

    def test_gamma_retry_after_is_honoured(self):
        calls: list[float] = []
        clock = {"now": 0.0}

        def gamma(slug: str) -> GammaStrike:
            del slug
            calls.append(clock["now"] - END)
            if len(calls) == 1:
                raise CryptoPriceHTTPError(429, retry_after=900.0)
            return GammaStrike(price_to_beat="100", final_price="101")

        service, _path, _memory = self._checked_service(
            gamma, open_ref="100", open_source=oracle_log.STRIKE_RTDS,
            close_twap="101", close_source=oracle_log.CLOSE_RTDS, end_logged=True,
        )
        rec = Recorder()
        self._run(service, clock, END, END + 1600, rec)
        self.assertEqual(calls, [600.0, 1500.0])
        self.assertEqual(rec.named("oracle_strike_check")[0]["match"], True)
        self.assertEqual(rec.named("oracle_close_check")[0]["match"], True)

    def test_unpublished_metadata_is_a_normal_wait_not_an_error(self):
        calls: list[float] = []
        clock = {"now": 0.0}

        def gamma(slug: str) -> GammaStrike:
            del slug
            calls.append(clock["now"] - END)
            if clock["now"] < END + 900:
                return GammaStrike(price_to_beat=None, final_price=None)
            if clock["now"] < END + 1100:
                return GammaStrike(price_to_beat="100", final_price=None)
            return GammaStrike(price_to_beat="100", final_price="101.5")

        service, path, memory = self._checked_service(
            gamma, open_ref="100", open_source=oracle_log.STRIKE_RTDS,
            close_twap="101.5", close_source=oracle_log.CLOSE_RTDS, end_logged=True,
        )
        rec = Recorder()
        self._run(service, clock, END, END + 1500, rec)
        self.assertEqual(calls, [600.0, 720.0, 840.0, 960.0, 1080.0, 1200.0])
        self.assertEqual(rec.fails, [])
        self.assertEqual(len(rec.named("oracle_strike_check")), 1)
        self.assertEqual(len(rec.named("oracle_close_check")), 1)
        self.assertEqual(memory.check_http.counts, {"unpublished": 3})
        self.assertFalse(any(row["event"] == "oracle_log_fail" for row in _rows(path)))

    def test_gamma_errors_log_once_per_window_per_kind(self):
        script = ["400", "400", "429", "400", "ok"]
        clock = {"now": 0.0}

        def gamma(slug: str) -> GammaStrike:
            del slug
            step = script.pop(0) if script else "ok"
            if step == "ok":
                return GammaStrike(price_to_beat="100", final_price="101")
            raise CryptoPriceHTTPError(int(step))

        service, _path, memory = self._checked_service(gamma, open_ref="100", close_twap="101")
        rec = Recorder()
        self._run(service, clock, END, END + 1400, rec)
        http_fails = [f for f in rec.fails if f.startswith("gamma")]
        self.assertEqual(len(http_fails), 2)
        self.assertIn("http_400", http_fails[0])
        self.assertIn("http_429", http_fails[1])
        self.assertEqual(memory.check_http.counts, {"http_400": 3, "http_429": 1})
        self.assertEqual(memory.check_http.attempts, 5)
        self.assertTrue(memory.check_done)

    def test_one_gamma_request_per_tick_across_windows(self):
        calls: list[str] = []

        def gamma(slug: str) -> GammaStrike:
            calls.append(slug)
            return GammaStrike(price_to_beat="100", final_price="101")

        feed = FakeFeed()
        service, _path = _service(feed, _open_only, fetch_gamma=gamma)
        for offset in (0.0, 900.0, 1800.0):
            window = oracle_log.OracleWindow(
                condition_id=f"cid-{int(offset)}", slug=f"btc-updown-15m-{int(START + offset)}",
                start_ts=START + offset, end_ts=END + offset,
            )
            service._memory[window.condition_id] = oracle_log._WindowMemory(
                window=window, open_ref="100", close_twap="101",
            )
        now = END + 1800 + GAMMA_FIRST_S
        service.tick({"intents": {}}, now, enabled=True)
        self.assertEqual(len(calls), 1)
        service.tick({"intents": {}}, now + 1, enabled=True)
        service.tick({"intents": {}}, now + 2, enabled=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(set(calls)), 3)

    def test_fallback_429_backs_off_and_stops_at_window_end(self):
        calls: list[float] = []
        clock = {"now": 0.0}

        def fetch(start_ts: int) -> WindowPrice:
            del start_ts
            calls.append(clock["now"] - START)
            raise CryptoPriceHTTPError(429)

        feed = FakeFeed()
        service, _path = _service(feed, fetch)
        rec = Recorder()
        for now in range(int(START), int(END + 60)):
            clock["now"] = float(now)
            feed.latest_sample = _sample(float(now) - 0.5)
            service.tick(_bag(), float(now), enabled=True, **rec.kwargs())
        self.assertEqual(calls[:5], [75.0, 95.0, 135.0, 215.0, 335.0])
        self.assertLess(max(calls), END - START)
        http_fails = [f for f in rec.fails if f.startswith("crypto-price")]
        self.assertEqual(len(http_fails), 1)
        self.assertIn("http_429", http_fails[0])
        self.assertEqual(len(rec.named("oracle_strike_late")), 1)

    def test_jitter_delays_the_first_request_and_stays_in_range(self):
        calls: list[float] = []
        clock = {"now": 0.0}

        def fetch(start_ts: int) -> WindowPrice:
            del start_ts
            calls.append(clock["now"] - START)
            return WindowPrice(open_ref="100", close_twap=None, completed=False)

        feed = FakeFeed()
        service, _path = _service(feed, fetch)
        service._jitter = lambda: 2.4
        for now in range(int(START - 5), int(START + 90)):
            clock["now"] = float(now)
            service.tick(_bag(), float(now), enabled=True)
        self.assertEqual(calls, [78.0])
        service._jitter = lambda: 99.0
        self.assertEqual(service._jitter_s(), 3.0)
        service._jitter = lambda: -1.0
        self.assertEqual(service._jitter_s(), 0.0)

    def test_one_request_in_flight_per_window(self):
        feed = FakeFeed()
        calls: list[int] = []
        holder: dict = {}

        def fetch(start_ts: int) -> WindowPrice:
            calls.append(start_ts)
            svc = holder["svc"]
            memory = svc._memory["cid-15m"]
            memory.open_http.next_at = 0.0
            svc._maybe_open_fallback(memory.window, memory, START + 200)
            return WindowPrice(open_ref=None, close_twap=None, completed=False)

        service, _path = _service(feed, fetch)
        holder["svc"] = service
        service.tick(_bag(), START + 100, enabled=True)
        self.assertEqual(len(calls), 1)
        self.assertFalse(service._memory["cid-15m"].open_http.inflight)

    def test_sampling_still_stops_at_grace_after_the_end(self):
        feed = FakeFeed()
        service, path = _service(feed, _open_only)
        sleeps: dict[float, float] = {}
        for now in range(int(END), int(END + CLOSE_GRACE_S + 30)):
            feed.latest_sample = _sample(float(now))
            feed.samples = [feed.latest_sample]
            service.tick(_bag(), float(now), enabled=True)
            sleeps[now - END] = service.sleep_s
        rows = _rows(path)
        ends = [row for row in rows if row["event"] == "oracle_window_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["twap_ts"], END)
        twap_ts = [row["twap_ts"] for row in rows if row["event"] == "oracle_twap"]
        self.assertLessEqual(max(twap_ts), END + GRACE_AFTER_S)
        self.assertEqual(sleeps[250], 5.0)
        self.assertEqual(feed.started, int(GRACE_AFTER_S) + 1)


if __name__ == "__main__":
    unittest.main()
