"""Strike / close capture from the RTDS boundary sample, and Gamma reconciliation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from buy import oracle_log
from buy.mint_sell import late_oracle_scrap_ok
from buy.oracle_log import (
    CLOSE_GRACE_S,
    GAMMA_FIRST_S,
    STRIKE_WAIT_S,
    GammaStrike,
    OracleHTTPError,
    OracleLogService,
    WindowPrice,
    boundary_second,
    fetch_gamma_strike,
    gamma_event_slug,
    parse_gamma_event_body,
    usd_diff,
)
from test_oracle_log import END, START, FakeFeed, _bag, _rows, _sample

STRIKE = "83900.66"


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


class Harness:
    def __init__(self, *, crypto_open: str | None = "83879.58", gamma=None) -> None:
        self.feed = FakeFeed()
        self.crypto_calls: list[float] = []
        self.gamma_calls: list[float] = []
        self.now = 0.0
        self.crypto_open = crypto_open
        self.gamma = gamma or (lambda _slug: GammaStrike(price_to_beat=None, final_price=None))
        folder = Path(tempfile.mkdtemp(prefix="oracle-strike-"))
        self.path = folder / "oracle_twap.jsonl"
        self.service = OracleLogService(
            self.path,
            feed=self.feed,
            fetch_price=self._fetch_price,
            fetch_gamma=self._fetch_gamma,
            jitter=lambda: 0.0,
        )
        self.rec = Recorder()

    def _fetch_price(self, start_ts: int) -> WindowPrice:
        self.crypto_calls.append(self.now - start_ts)
        return WindowPrice(open_ref=self.crypto_open, close_twap="1", completed=True)

    def _fetch_gamma(self, slug: str) -> GammaStrike:
        self.gamma_calls.append(self.now)
        return self.gamma(slug)

    def tick(self, now: float, samples=(), state=None) -> None:
        self.now = float(now)
        self.feed.samples = list(samples)
        if samples:
            self.feed.latest_sample = max(samples, key=lambda s: s.obs_ts)
        self.service.tick(_bag() if state is None else state, float(now), enabled=True, **self.rec.kwargs())

    def rows(self, event: str) -> list[dict]:
        return [row for row in _rows(self.path) if row["event"] == event]


class BoundaryHelperTests(unittest.TestCase):
    def test_boundary_second_is_exact_15m_only(self):
        self.assertEqual(boundary_second(START), int(START))
        self.assertEqual(boundary_second(START + 0.0001), int(START))
        self.assertIsNone(boundary_second(START + 0.5))
        self.assertIsNone(boundary_second(START + 1))
        self.assertIsNone(boundary_second(START + 60))
        self.assertIsNone(boundary_second(None))
        self.assertEqual(gamma_event_slug(START), f"btc-updown-15m-{int(START)}")

    def test_usd_diff_is_exact(self):
        self.assertEqual(usd_diff("83879.58", "83900.66"), oracle_log.Decimal("-21.08"))
        self.assertIsNone(usd_diff(None, "1"))
        self.assertIsNone(usd_diff("x", "1"))


class StrikeCaptureTests(unittest.TestCase):
    def test_strike_is_the_twap_sample_stamped_at_window_start(self):
        h = Harness()
        h.tick(START - 1, [_sample(START - 1, "83890")])
        h.tick(START + 1.4, [_sample(START, STRIKE), _sample(START + 1, "83901")])
        view = h.service.bag_view("cid-15m")
        self.assertEqual(view.open_usd, STRIKE)
        self.assertEqual(view.open_source, "rtds_twap_at_start")
        rows = h.rows("oracle_open_ref")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["open_ref"], STRIKE)
        self.assertEqual(rows[0]["twap_ts"], START)
        self.assertEqual(rows[0]["strike_source"], "rtds_twap_at_start")
        self.assertEqual(rows[0]["capture_delay_s"], 1.4)
        for now in range(int(START + 2), int(START + 200)):
            h.tick(now, [_sample(now - 1, "83905")])
        self.assertEqual(h.crypto_calls, [])
        self.assertEqual(h.rec.named("oracle_strike_late"), [])
        twaps = [r for r in h.rows("oracle_twap") if r["twap_ts"] > START]
        self.assertTrue(all(r["open_ref"] == STRIKE for r in twaps))

    def test_duplicate_boundary_message_is_a_no_op(self):
        h = Harness()
        h.tick(START + 1, [_sample(START, STRIKE)])
        h.tick(START + 2, [_sample(START, STRIKE), _sample(START + 1, "83901")])
        h.tick(START + 30, [_sample(START, STRIKE), _sample(START + 29, "83902")])
        self.assertEqual(len(h.rows("oracle_open_ref")), 1)
        self.assertEqual(h.rec.named("oracle_strike_revised"), [])
        at_start = [r for r in h.rows("oracle_twap") if r["twap_ts"] == START]
        self.assertEqual(len(at_start), 1)

    def test_revised_boundary_message_supersedes_and_is_logged(self):
        h = Harness()
        h.tick(START + 1, [_sample(START, "83900.62")])
        h.tick(START + 2, [_sample(START, STRIKE), _sample(START + 1, "83901")])
        self.assertEqual(h.service.bag_view("cid-15m").open_usd, "83900.62")
        rows = h.rows("oracle_open_ref")
        self.assertEqual([r["open_ref"] for r in rows], ["83900.62"])
        self.assertEqual(h.rec.named("oracle_strike_revised"), [])
        at_start = [r["twap"] for r in h.rows("oracle_twap") if r["twap_ts"] == START]
        self.assertEqual(at_start, ["83900.62", STRIKE])

    def test_revision_inside_one_batch_keeps_the_last_arrival(self):
        h = Harness()
        h.tick(START + 1, [_sample(START, "83900.5"), _sample(START, STRIKE)])
        self.assertEqual(h.service.bag_view("cid-15m").open_usd, "83900.5")
        self.assertEqual(len(h.rows("oracle_open_ref")), 1)

    def test_late_capture_after_reconnect_replays_the_boundary(self):
        h = Harness()
        h.tick(START - 3, [_sample(START - 3, "83895")])
        for now in range(int(START - 2), int(START + 40)):
            h.tick(now)
        replay = [_sample(START + dt, f"{83900 + dt}.5") for dt in range(-17, 39)]
        replay[17] = _sample(START, STRIKE)
        h.tick(START + 40, replay)
        view = h.service.bag_view("cid-15m")
        self.assertEqual(view.open_usd, STRIKE)
        self.assertEqual(view.open_source, "rtds_twap_at_start")
        row = h.rows("oracle_open_ref")[0]
        self.assertEqual(row["capture_delay_s"], 40.0)
        for now in range(int(START + 41), int(START + 120)):
            h.tick(now, [_sample(now - 1)])
        self.assertEqual(h.crypto_calls, [])
        self.assertEqual(h.rec.named("oracle_strike_late"), [])

    def test_previous_window_end_sample_is_the_next_strike(self):
        h = Harness()
        nxt = {
            "status": "submitting",
            "slug": f"btc-updown-15m-{int(END)}",
            "condition_id": "cid-next",
            "series_slug": "btc-up-or-down-15m",
        }
        state = _bag()
        state["intents"]["cid-next"] = nxt
        h.tick(END + 1.2, [_sample(END, STRIKE)], state=state)
        self.assertEqual(h.service.bag_view("cid-next").open_usd, STRIKE)
        end_rows = h.rows("oracle_window_end")
        self.assertEqual(len(end_rows), 1)
        self.assertEqual(end_rows[0]["condition_id"], "cid-15m")
        self.assertEqual(end_rows[0]["twap"], STRIKE)

    def test_no_boundary_by_wait_falls_back_to_crypto_price_labelled(self):
        h = Harness()
        for now in range(int(START + 1), int(START + STRIKE_WAIT_S + 5)):
            h.tick(now, [_sample(now - 0.6)])
        self.assertEqual(h.crypto_calls, [STRIKE_WAIT_S])
        late = h.rec.named("oracle_strike_late")
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["reason"], "no_boundary_sample")
        self.assertEqual(late[0]["fallback"], "crypto_price_open")
        rows = h.rows("oracle_open_ref")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["open_ref"], "83879.58")
        self.assertEqual(rows[0]["source"], "polymarket_crypto_price")
        self.assertEqual(rows[0]["strike_source"], "crypto_price_open")
        view = h.service.bag_view("cid-15m")
        self.assertEqual(view.open_source, "crypto_price_open")
        for now in range(int(START + 80), int(START + 300)):
            h.tick(now, [_sample(now - 0.6)])
        self.assertEqual(len(h.crypto_calls), 1)

        h.tick(START + 301, [_sample(START, STRIKE)])
        view = h.service.bag_view("cid-15m")
        self.assertEqual(view.open_usd, STRIKE)
        self.assertEqual(view.open_source, "rtds_twap_at_start")
        revised = h.rec.named("oracle_strike_revised")
        self.assertEqual(revised[0]["previous_source"], "crypto_price_open")
        self.assertAlmostEqual(revised[0]["delta"], 21.08)

    def test_fallback_is_not_used_before_the_wait_or_after_the_end(self):
        h = Harness()
        for now in range(int(START - 30), int(START + STRIKE_WAIT_S)):
            h.tick(now, [_sample(now - 0.6)])
        self.assertEqual(h.crypto_calls, [])
        late = Harness()
        late.tick(END + 1, [_sample(END + 0.4)])
        late.tick(END + 2, [_sample(END + 1.4)])
        self.assertEqual(late.crypto_calls, [])
        self.assertIsNone(late.service.bag_view("cid-15m").open_usd)

    def test_missing_end_sample_logs_window_end_missed_once(self):
        h = Harness()
        h.tick(START + 1, [_sample(START, STRIKE)])
        for now in range(int(END - 2), int(END + CLOSE_GRACE_S + 30)):
            h.tick(now, [_sample(now - 0.6)])
        self.assertEqual(h.rows("oracle_window_end"), [])
        missed = h.rec.named("oracle_window_end_missed")
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["outcome"], "no_boundary_sample")


class GammaReconcileTests(unittest.TestCase):
    def _run_window(self, h: Harness, *, strike_sample: bool, close_value: str = "84010.25"):
        if strike_sample:
            h.tick(START + 1, [_sample(START, STRIKE)])
        else:
            for now in range(int(START + 1), int(START + STRIKE_WAIT_S + 2)):
                h.tick(now, [_sample(now - 0.6)])
        h.tick(END + 1.3, [_sample(END, close_value)])
        for now in range(int(END + 2), int(END + GAMMA_FIRST_S + 1)):
            h.tick(now)

    def test_no_gamma_request_before_publication_delay(self):
        h = Harness(gamma=lambda _s: GammaStrike(price_to_beat=STRIKE, final_price="84010.25"))
        h.tick(START + 1, [_sample(START, STRIKE)])
        h.tick(END + 1, [_sample(END, "84010.25")])
        for now in range(int(END + 2), int(END + GAMMA_FIRST_S)):
            h.tick(now)
        self.assertEqual(h.gamma_calls, [])

    def test_matching_strike_and_close_log_a_check_without_correction(self):
        h = Harness(gamma=lambda _s: GammaStrike(price_to_beat="83900.664", final_price="84010.25"))
        self._run_window(h, strike_sample=True)
        self.assertEqual(h.gamma_calls, [END + GAMMA_FIRST_S])
        check = h.rec.named("oracle_strike_check")
        self.assertEqual(len(check), 1)
        self.assertTrue(check[0]["match"])
        self.assertEqual(check[0]["strike_source"], "rtds_twap_at_start")
        self.assertEqual(check[0]["price_to_beat"], "83900.664")
        self.assertAlmostEqual(check[0]["diff"], -0.004)
        self.assertEqual(check[0]["capture_delay_s"], 1.0)
        close = h.rec.named("oracle_close_check")
        self.assertTrue(close[0]["match"])
        self.assertEqual(len(h.rows("oracle_open_ref")), 1)
        self.assertEqual(len(h.rows("oracle_window_end")), 1)

    def test_mismatched_fallback_strike_is_corrected_from_gamma(self):
        h = Harness(gamma=lambda _s: GammaStrike(price_to_beat=STRIKE, final_price="84012"))
        self._run_window(h, strike_sample=False)
        check = h.rec.named("oracle_strike_check")[0]
        self.assertFalse(check["match"])
        self.assertEqual(check["strike"], "83879.58")
        self.assertEqual(check["strike_source"], "crypto_price_open")
        self.assertAlmostEqual(check["diff"], -21.08)
        rows = h.rows("oracle_open_ref")
        self.assertEqual(rows[-1]["open_ref"], STRIKE)
        self.assertEqual(rows[-1]["strike_source"], "gamma_price_to_beat")
        self.assertEqual(rows[-1]["source"], "gamma_event_metadata")
        self.assertEqual(rows[-1]["previous"], "83879.58")
        self.assertEqual(h.service.bag_view("cid-15m").open_source, "gamma_price_to_beat")
        self.assertEqual(h.rec.named("oracle_strike_revised"), [])
        ends = h.rows("oracle_window_end")
        self.assertEqual([r["close_source"] for r in ends], ["rtds_twap_at_end", "gamma_final_price"])
        self.assertEqual(ends[-1]["twap"], "84012")
        self.assertFalse(h.rec.named("oracle_close_check")[0]["match"])

    def test_late_rtds_sample_does_not_overwrite_a_gamma_correction(self):
        h = Harness(gamma=lambda _s: GammaStrike(price_to_beat=STRIKE, final_price="84010.25"))
        self._run_window(h, strike_sample=False)
        h.service._note_boundaries([_sample(START, "1")], None)
        memory = h.service._memory["cid-15m"]
        h.service._resolve_boundaries(memory.window, memory, END + 900)
        self.assertEqual(h.service.bag_view("cid-15m").open_usd, STRIKE)


class GammaParserTests(unittest.TestCase):
    def test_parses_event_metadata_object_or_string(self):
        body = json.dumps(
            [{"slug": "btc-updown-15m-1790857800", "eventMetadata": {"priceToBeat": 83900.66, "finalPrice": 84010.2534}}]
        )
        self.assertEqual(
            parse_gamma_event_body(body), GammaStrike(price_to_beat="83900.66", final_price="84010.2534")
        )
        nested = json.dumps([{"eventMetadata": json.dumps({"priceToBeat": "83900.66"})}])
        self.assertEqual(parse_gamma_event_body(nested), GammaStrike(price_to_beat="83900.66", final_price=None))

    def test_unpublished_or_bad_metadata_is_none(self):
        for body in ("[]", "[{}]", '[{"eventMetadata": null}]', '[{"eventMetadata": "nope"}]',
                     '[{"eventMetadata": {"priceToBeat": 0, "finalPrice": true}}]'):
            self.assertEqual(parse_gamma_event_body(body), GammaStrike(price_to_beat=None, final_price=None), body)
        with self.assertRaises(ValueError):
            parse_gamma_event_body('"text"')

    def test_fetch_uses_slug_param_and_raises_with_retry_after(self):
        seen: dict = {}

        def get(url, **kw):
            seen["url"] = url
            seen["params"] = kw.get("params")
            return SimpleNamespace(status_code=429, headers={"Retry-After": "40"}, text="slow")

        with mock.patch.object(oracle_log, "thread_session", lambda _name: SimpleNamespace(get=get)):
            with self.assertRaises(OracleHTTPError) as caught:
                fetch_gamma_strike("btc-updown-15m-1790857800")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 40.0)
        self.assertEqual(seen["url"], "https://gamma-api.polymarket.com/events")
        self.assertEqual(seen["params"], {"slug": "btc-updown-15m-1790857800"})
        ok = SimpleNamespace(status_code=200, headers={}, text='[{"eventMetadata":{"priceToBeat":1.5}}]')
        with mock.patch.object(oracle_log, "thread_session", lambda _name: SimpleNamespace(get=lambda *a, **k: ok)):
            self.assertEqual(fetch_gamma_strike("x"), GammaStrike(price_to_beat="1.5", final_price=None))


class VetoUnaffectedTests(unittest.TestCase):
    def test_default_config_never_reads_the_strike(self):
        for open_usd in (None, "83879.58", STRIKE):
            ok, why, detail = late_oracle_scrap_ok(
                ttm_s=30.0, scrap_leg="up", twap_usd="83800", open_usd=open_usd, twap_age_s=1.0,
                late_window_s=0.0, edge_per_ttm=0.0, floor_usd=0.0, stale_s=0.0,
            )
            self.assertEqual((ok, why), (True, "outside_late_window"))
            self.assertIsNone(detail["open_usd"])


if __name__ == "__main__":
    unittest.main()
