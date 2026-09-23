"""Chainlink TWAP tape: parser, writer, and decision-path isolation."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from buy.mint_sell import (
    classify_loser,
    cycle_sleep_s,
    sell_fire_decision,
    winner_sell_limit,
)
from buy.oracle_log import (
    CRYPTO_PRICE_VARIANT,
    OracleBagView,
    OracleLogService,
    RtdsTwapFeed,
    TwapSample,
    WindowPrice,
    append_jsonl,
    crypto_price_params,
    e18_to_decimal_str,
    notes_for,
    parse_crypto_price_body,
    parse_rtds_message,
    sample_interval_s,
    snapshot_intents,
    windows_from_intents,
)


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
BUY = ROOT / "buy"
START = 1_790_063_100.0
END = START + 900.0

DECISION_FNS = (
    "manage_sells",
    "_manage_sells_locked",
    "run_sell_cycle",
    "run_mint_cycle",
    "eligible_markets",
    "already_minted",
    "mint_slots_full",
    "open_intent_count",
    "_fak_sell",
    "_sell_inventory",
    "_run_fak_ladder",
    "_run_dump_fak_with_refire",
    "_apply_sell_fire_cancel",
)

MINT_ONLY_FNS = (
    "run_mint_cycle",
    "eligible_markets",
    "already_minted",
    "mint_slots_full",
    "open_intent_count",
)

SELL_ORACLE_FNS = (
    "manage_sells",
    "_manage_sells_locked",
)

FORBIDDEN_MINT = (
    "oracle_log",
    "oracle_twap",
    "OracleLog",
    "crypto_prices_twap",
    "chainlink",
    "open_ref",
    "oracle_log_fail",
    "late_oracle",
    "bag_view",
    "sell_loser_oracle",
)

# manage_sells may read bag_view for the late scrap veto only.
FORBIDDEN_SELL_CYCLE = (
    "crypto_prices_twap",
    "chainlink",
)

# Pure mint_sell helpers stay free of the feed / websocket / jsonl path.
FORBIDDEN_MINT_SELL = (
    "oracle_log",
    "OracleLog",
    "crypto_prices_twap",
    "chainlink",
    "oracle_log_fail",
    "oracle_twap.jsonl",
)


def _fn_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(text, node)
            if segment:
                return segment
    raise AssertionError(f"missing {name} in {path.name}")


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class FakeFeed:
    def __init__(self) -> None:
        self.samples: list[TwapSample] = []
        self.latest_sample: TwapSample | None = None
        self.error = ""
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def drain(self) -> list[TwapSample]:
        out = list(self.samples)
        self.samples.clear()
        return out

    def latest(self) -> TwapSample | None:
        return self.latest_sample

    def last_error(self) -> str:
        return self.error


def _sample(obs: float, twap: str = "85260.062350763205066752") -> TwapSample:
    return TwapSample(symbol="btc/usd", window_s=60, twap=twap, obs_ts=obs)


def _bag(**extra: object) -> dict:
    intent = {
        "status": "confirmed",
        "slug": f"btc-updown-15m-{int(START)}",
        "condition_id": "cid-15m",
        "series_slug": "btc-up-or-down-15m",
        "start_ts": START,
        "end_ts": END,
        "shares": 5,
    }
    intent.update(extra)
    return {"intents": {"cid-15m": intent}}


class ParserTests(unittest.TestCase):
    def test_e18_keeps_chainlink_precision(self):
        raw = "85260062350763205066752"
        self.assertEqual(e18_to_decimal_str(raw), "85260.062350763205066752")
        self.assertEqual(e18_to_decimal_str("65000500000000000000000"), "65000.5")
        self.assertIsNone(e18_to_decimal_str("12.5"))

    def test_update_frame_uses_full_accuracy_value(self):
        frame = {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "timestamp": 1790063445365,
            "payload": {
                "symbol": "btc/usd",
                "timestamp": 1790063444000,
                "value": 85260.07,
                "window_s": 60,
                "full_accuracy_value": "85260062350763205066752",
            },
        }
        samples = parse_rtds_message(json.dumps(frame))
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].twap, "85260.062350763205066752")
        self.assertEqual(samples[0].obs_ts, 1790063444.0)
        self.assertEqual(samples[0].window_s, 60)
        self.assertEqual(samples[0].symbol, "btc/usd")

    def test_subscribe_backlog_is_the_recent_path(self):
        frame = {
            "topic": "crypto_prices_twap_sixty",
            "type": "subscribe",
            "timestamp": 1790063444080,
            "payload": {
                "symbol": "btc/usd",
                "window_s": 60,
                "data": [
                    {
                        "full_accuracy_value": "85260000000000000000000",
                        "timestamp": 1790063440000,
                        "value": 85260.0,
                    },
                    {
                        "full_accuracy_value": "85261000000000000000000",
                        "timestamp": 1790063441000,
                        "value": 85261.0,
                    },
                ],
            },
        }
        samples = parse_rtds_message(json.dumps(frame))
        self.assertEqual([sample.twap for sample in samples], ["85260", "85261"])
        self.assertEqual([sample.obs_ts for sample in samples], [1790063440.0, 1790063441.0])

    def test_ignores_pong_other_symbol_and_thirty_second_topic(self):
        self.assertEqual(parse_rtds_message(""), [])
        self.assertEqual(parse_rtds_message("PONG"), [])
        thirty = {
            "topic": "crypto_prices_twap_thirty",
            "type": "update",
            "payload": {
                "symbol": "btc/usd",
                "timestamp": 1790063444000,
                "window_s": 30,
                "full_accuracy_value": "85260000000000000000000",
            },
        }
        eth = {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": 1790063444000,
                "window_s": 60,
                "full_accuracy_value": "1000000000000000000000",
            },
        }
        self.assertEqual(parse_rtds_message(json.dumps(thirty)), [])
        self.assertEqual(parse_rtds_message(json.dumps(eth)), [])

    def test_crypto_price_open_and_completed_close(self):
        incomplete = parse_crypto_price_body(
            '{"openPrice":85224.17742358848,"closePrice":null,"completed":false,"incomplete":true}'
        )
        self.assertEqual(incomplete.open_ref, "85224.17742358848")
        self.assertIsNone(incomplete.close_twap)
        self.assertFalse(incomplete.completed)
        done = parse_crypto_price_body(
            '{"openPrice":85295.61331191272,"closePrice":85224.17742358848,'
            '"completed":true,"incomplete":false}'
        )
        self.assertEqual(done.close_twap, "85224.17742358848")
        self.assertTrue(done.completed)
        params = crypto_price_params(int(START))
        self.assertEqual(params["variant"], "fifteen")
        self.assertEqual(params["variant"], CRYPTO_PRICE_VARIANT)
        self.assertEqual(params["symbol"], "btc")
        self.assertNotIn("fifteenminute", params["variant"])

    def test_feed_handle_message_drains_once(self):
        feed = RtdsTwapFeed()
        feed.handle_message(
            json.dumps(
                {
                    "topic": "crypto_prices_twap_sixty",
                    "type": "update",
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp": 1790063445000,
                        "window_s": 60,
                        "full_accuracy_value": "85261000000000000000000",
                    },
                }
            )
        )
        feed.handle_message('{"error":"topic not found"}')
        drained = feed.drain()
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].twap, "85261")
        self.assertEqual(feed.drain(), [])
        self.assertEqual(feed.latest().obs_ts, 1790063445.0)
        self.assertIn("topic not found", feed.last_error())


class WindowAndCadenceTests(unittest.TestCase):
    def test_tracks_open_bags_and_skips_failed_or_other_series(self):
        now = START + 400
        state = _bag()
        state["intents"]["cid-15m"]["twap"] = "999"
        state["intents"]["cid-15m"]["open_ref"] = "1"
        state["intents"]["failed"] = {
            "status": "failed",
            "slug": f"btc-updown-15m-{int(START)}",
            "condition_id": "failed",
            "series_slug": "btc-up-or-down-15m",
            "start_ts": START,
            "end_ts": END,
        }
        state["intents"]["hourly"] = {
            "status": "confirmed",
            "slug": "btc-updown-hourly-1",
            "condition_id": "hourly",
            "series_slug": "btc-up-or-down-hourly",
            "start_ts": START,
            "end_ts": START + 3600,
        }
        state["intents"]["soon"] = {
            "status": "submitting",
            "slug": f"btc-updown-15m-{int(START + 900)}",
            "condition_id": "soon",
            "series_slug": "btc-up-or-down-15m",
            "start_ts": START + 900,
            "end_ts": START + 1800,
        }
        windows = windows_from_intents(state, now)
        self.assertEqual([item.condition_id for item in windows], ["cid-15m", "soon"])
        self.assertEqual(
            [item.condition_id for item in windows_from_intents(state, END + 30)],
            ["cid-15m", "soon"],
        )
        self.assertEqual(
            [item.condition_id for item in windows_from_intents(state, END + 121)],
            ["soon"],
        )

    def test_intervals_are_dense_near_the_end_and_slow_in_the_middle(self):
        self.assertEqual(sample_interval_s(START + 400, START, END), 15.0)
        self.assertEqual(sample_interval_s(START + 5, START, END), 2.0)
        self.assertEqual(sample_interval_s(END - 90, START, END), 2.0)
        self.assertEqual(sample_interval_s(END - 10, START, END), 1.0)
        self.assertEqual(sample_interval_s(END + 10, START, END), 1.0)
        self.assertEqual(notes_for(END - 10, START, END), "last_min")
        self.assertEqual(notes_for(END + 1, START, END), "end")
        self.assertEqual(notes_for(START + 1, START, END), "open")
        self.assertEqual(notes_for(START + 400, START, END), "cold")


class WriterTests(unittest.TestCase):
    def test_append_jsonl_is_one_file(self):
        root = Path(tempfile.mkdtemp(prefix="oracle-log-"))
        target = root / "logs" / "oracle_twap.jsonl"
        sibling = root / "positions_mint.json"
        sibling.write_text('{"intents":{}}\n', encoding="utf-8")
        append_jsonl(
            target,
            {
                "ts": 1,
                "event": "oracle_twap",
                "slug": "btc-updown-15m-1",
                "condition_id": "cid",
                "window_start": 1,
                "window_end": 901,
                "source": "polymarket_rtds",
                "twap": "1.5",
                "open_ref": None,
                "notes": "cold",
            },
        )
        self.assertEqual(sibling.read_text(encoding="utf-8"), '{"intents":{}}\n')
        row = _rows(target)[0]
        for key in (
            "ts",
            "slug",
            "condition_id",
            "window_start",
            "window_end",
            "source",
            "twap",
            "open_ref",
            "notes",
        ):
            self.assertIn(key, row)

    def _service(self, feed: FakeFeed, fetch):
        folder = Path(tempfile.mkdtemp(prefix="oracle-log-"))
        path = folder / "oracle_twap.jsonl"
        return OracleLogService(path, feed=feed, fetch_price=fetch), path

    def test_bag_view_reuses_feed_and_open_ref(self):
        feed = FakeFeed()
        now = START + 100
        feed.latest_sample = _sample(now - 1.0, "85260.5")

        def fetch(start_ts: int) -> WindowPrice:
            return WindowPrice(open_ref="85224.5", close_twap=None, completed=False)

        svc, _path = self._service(feed, fetch)
        svc.tick(_bag(), now, enabled=True)
        view = svc.bag_view("cid-15m")
        self.assertIsInstance(view, OracleBagView)
        self.assertEqual(view.twap, "85260.5")
        self.assertEqual(view.open_usd, "85224.5")
        self.assertAlmostEqual(view.obs_ts, now - 1.0)
        missing = svc.bag_view("unknown-cid")
        self.assertEqual(missing.twap, "85260.5")
        self.assertIsNone(missing.open_usd)

    def test_hot_path_records_samples_without_touching_intents(self):
        feed = FakeFeed()
        now = END - 10
        feed.samples = [_sample(now - 2, "85260.1"), _sample(now - 1, "85260.2")]
        feed.latest_sample = feed.samples[-1]
        calls = []

        def fetch(start_ts: int) -> WindowPrice:
            calls.append(start_ts)
            return WindowPrice(open_ref="85224.5", close_twap=None, completed=False)

        service, path = self._service(feed, fetch)
        state = _bag(oracle_twap="nope", open_ref="nope")
        before = json.dumps(state, sort_keys=True)
        fails: list[str] = []
        service.tick(state, now, enabled=True, on_fail=fails.append)
        self.assertEqual(json.dumps(state, sort_keys=True), before)
        self.assertEqual(fails, [])
        self.assertEqual(calls, [int(START)])
        self.assertEqual(feed.started, 1)
        rows = _rows(path)
        self.assertEqual(rows[0]["event"], "oracle_open_ref")
        self.assertEqual(rows[0]["source"], "polymarket_crypto_price")
        self.assertEqual(rows[0]["open_ref"], "85224.5")
        twaps = [row for row in rows if row["event"] == "oracle_twap"]
        self.assertEqual([row["twap"] for row in twaps], ["85260.1", "85260.2"])
        self.assertEqual(twaps[0]["notes"], "last_min")
        self.assertEqual(twaps[0]["slug"], f"btc-updown-15m-{int(START)}")
        self.assertEqual(twaps[0]["condition_id"], "cid-15m")
        self.assertEqual(twaps[0]["window_start"], START)
        self.assertEqual(twaps[0]["window_end"], END)
        self.assertEqual(twaps[0]["open_ref"], "85224.5")
        self.assertEqual(service.sleep_s, 1.0)

    def test_cold_cadence_keeps_one_sample_per_interval(self):
        feed = FakeFeed()
        now = START + 400
        feed.latest_sample = _sample(now, "100")
        feed.samples = [_sample(now - 10, "90"), feed.latest_sample]

        def fetch(start_ts: int) -> WindowPrice:
            del start_ts
            return WindowPrice(open_ref="100", close_twap=None, completed=False)

        service, path = self._service(feed, fetch)
        service.tick(_bag(), now, enabled=True)
        feed.latest_sample = _sample(now + 5, "101")
        feed.samples = [feed.latest_sample]
        service.tick(_bag(), now + 5, enabled=True)
        feed.latest_sample = _sample(now + 15, "102")
        feed.samples = [feed.latest_sample]
        service.tick(_bag(), now + 15, enabled=True)
        twaps = [row["twap"] for row in _rows(path) if row["event"] == "oracle_twap"]
        self.assertEqual(twaps, ["100", "102"])
        self.assertEqual(service.sleep_s, 1.0)

    def test_window_end_row_and_http_failure_does_not_drop_the_tape(self):
        feed = FakeFeed()
        now = END + 5
        feed.latest_sample = _sample(now, "85200")
        feed.samples = [feed.latest_sample]
        fails: list[str] = []

        def fetch(start_ts: int) -> WindowPrice:
            del start_ts
            raise RuntimeError("oracle down")

        service, path = self._service(feed, fetch)
        state = _bag()
        before = json.dumps(state, sort_keys=True)
        service.tick(state, now, enabled=True, on_fail=fails.append)
        self.assertEqual(json.dumps(state, sort_keys=True), before)
        self.assertTrue(any("oracle down" in item for item in fails))
        rows = _rows(path)
        self.assertTrue(any(row["event"] == "oracle_twap" and row["notes"] == "end" for row in rows))
        self.assertTrue(any(row["event"] == "oracle_log_fail" for row in rows))
        self.assertFalse(any(row["event"] == "oracle_window_end" for row in rows))

        def fetch_done(start_ts: int) -> WindowPrice:
            del start_ts
            return WindowPrice(
                open_ref="85224.5",
                close_twap="85200.25",
                completed=True,
            )

        service._fetch_price = fetch_done
        service.tick(state, now + 20, enabled=True, on_fail=fails.append)
        end_rows = [row for row in _rows(path) if row["event"] == "oracle_window_end"]
        self.assertEqual(len(end_rows), 1)
        self.assertEqual(end_rows[0]["twap"], "85200.25")
        self.assertEqual(end_rows[0]["open_ref"], "85224.5")
        self.assertEqual(end_rows[0]["source"], "polymarket_crypto_price")
        self.assertEqual(json.dumps(state, sort_keys=True), before)

    def test_disabled_flag_does_not_connect_or_write(self):
        feed = FakeFeed()
        feed.latest_sample = _sample(START + 10, "1")
        service, path = self._service(feed, lambda _start: (_ for _ in ()).throw(AssertionError("fetch")))
        service.tick(_bag(), START + 10, enabled=False)
        self.assertEqual(feed.started, 0)
        self.assertFalse(path.exists())
        self.assertGreaterEqual(feed.stopped, 1)

    def test_snapshot_does_not_alias_live_intents(self):
        state = _bag()
        snap = snapshot_intents(state)
        snap["intents"]["cid-15m"]["status"] = "failed"
        self.assertEqual(state["intents"]["cid-15m"]["status"], "confirmed")


class DecisionIsolationTests(unittest.TestCase):
    def test_sell_policy_ignores_intent_oracle_fields(self):
        now = START + 400
        intent = _bag()["intents"]["cid-15m"]
        intent["sell_loser_armed_at"] = 1
        intent["twap"] = "1"
        intent["open_ref"] = "2"
        intent["oracle_twap"] = "3"
        cfg = {
            "poll_s": 5.0,
            "sell_armed_poll_s": 2.0,
            "oracle_log_enabled": False,
        }
        state = {"intents": {"cid-15m": intent}}
        self.assertEqual(cycle_sleep_s(cfg, state, now), 2.0)
        cfg["oracle_log_enabled"] = True
        self.assertEqual(cycle_sleep_s(cfg, state, now), 2.0)
        self.assertEqual(
            classify_loser(0.02, 0.95, threshold=0.03, opposite_min=0.90),
            ("up", "loser"),
        )
        self.assertEqual(
            sell_fire_decision(
                "loser",
                bid=0.02,
                opposite_bid=0.95,
                threshold=0.03,
                floor=0.02,
                opposite_min=0.90,
            )[0],
            "fire",
        )
        self.assertEqual(winner_sell_limit(0.995)[0], 0.99)

    def test_mint_path_does_not_reference_the_oracle(self):
        for name in MINT_ONLY_FNS:
            source = _fn_source(MINT, name)
            for token in FORBIDDEN_MINT:
                self.assertNotIn(token, source, f"{name} contains {token}")
        for name in ("_fak_sell", "_sell_inventory", "_run_fak_ladder", "_run_dump_fak_with_refire"):
            source = _fn_source(MINT, name)
            for token in FORBIDDEN_MINT:
                self.assertNotIn(token, source, f"{name} contains {token}")
        mint_sell = (BUY / "mint_sell.py").read_text(encoding="utf-8")
        for token in FORBIDDEN_MINT_SELL:
            self.assertNotIn(token, mint_sell, f"mint_sell.py contains {token}")
        self.assertIn("def late_oracle_scrap_ok", mint_sell)
        loops = (BUY / "mint_loops.py").read_text(encoding="utf-8")
        for token in FORBIDDEN_MINT:
            self.assertNotIn(token, loops, f"mint_loops.py contains {token}")
        oracle_src = (BUY / "oracle_log.py").read_text(encoding="utf-8")
        for token in ("manage_sells", "classify_loser", "sell_fire_decision", "submit_mint"):
            self.assertNotIn(token, oracle_src)
        tree = ast.parse(oracle_src, filename="oracle_log.py")
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertNotIn("mintbot", imported)
        self.assertNotIn("buy.mint_sell", imported)
        main = _fn_source(MINT, "main")
        self.assertIn("OracleLogService", main)
        self.assertIn("_set_oracle_service", main)
        self.assertIn("oracle_log_fail", main)
        self.assertIn("mintbot-oracle", main)
        self.assertIn("oracle_log_enabled", main)
        self.assertNotIn("≤120s TTM", main)
        self.assertIn("audit only; scrap veto off", main)
        defaults = _fn_source(MINT, "load_strategy")
        self.assertNotIn("oracle_twap", defaults)
        example = json.loads((ROOT / "strategy_mint.example.json").read_text(encoding="utf-8"))
        mint_tree = ast.parse(MINT.read_text(encoding="utf-8"), filename="mintbot.py")
        defaults_value = None
        for node in mint_tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "DEFAULTS":
                        defaults_value = ast.literal_eval(node.value)
        self.assertIs(defaults_value["oracle_log_enabled"], True)
        self.assertEqual(defaults_value["sell_late_window_s"], 0.0)
        self.assertEqual(defaults_value["sell_oracle_edge_per_ttm"], 0.0)
        self.assertEqual(defaults_value["sell_oracle_edge_persist_s"], 3.0)
        self.assertEqual(defaults_value["sell_oracle_stale_s"], 0.0)
        self.assertEqual(defaults_value["sell_oracle_edge_floor_usd"], 0.0)
        self.assertIs(example["oracle_log_enabled"], True)
        self.assertEqual(example["sell_late_window_s"], 0.0)
        self.assertEqual(example["sell_oracle_edge_per_ttm"], 0.0)
        self.assertEqual(example["sell_oracle_edge_persist_s"], 3.0)
        self.assertEqual(example["sell_oracle_stale_s"], 0.0)
        self.assertEqual(example["sell_oracle_edge_floor_usd"], 0.0)

    def test_manage_sells_wires_late_oracle_veto_only(self):
        manage = _fn_source(MINT, "_manage_sells_locked")
        self.assertIn("late_oracle_scrap_ok", manage)
        self.assertIn("sell_loser_oracle_block", manage)
        self.assertIn("sell_loser_oracle_ok", manage)
        self.assertIn("_oracle_bag_view", manage)
        self.assertIn("sell_late_window_s", manage)
        self.assertIn("late_window_s > 0", manage)
        for token in FORBIDDEN_SELL_CYCLE:
            self.assertNotIn(token, manage, f"_manage_sells_locked contains {token}")
        # Winner / dump / mint must not grow a second oracle strategy.
        self.assertNotIn("late_oracle_scrap_ok", _fn_source(MINT, "run_mint_cycle"))
        dump_tail = manage[manage.find("Held-leg dump") : manage.find("loser_persist_s")]
        self.assertNotIn("late_oracle_scrap_ok", dump_tail)


if __name__ == "__main__":
    unittest.main()
