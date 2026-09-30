"""WhatsApp (CallMeBot) scrap / dump alerts: format, no-op, non-blocking, redaction."""

from __future__ import annotations

import ast
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from buy.mint_gas import validate_mint_gas
from buy.whatsapp_notify import (
    CALLMEBOT_URL,
    BagAlerts,
    WhatsAppNotifier,
    bag_start_label,
    dump_message,
    redact,
    scrap_message,
)
from test_mint_cpu import (
    _dump_cfg,
    _dump_harness,
    _fill_fak,
    _held_after_scrap,
    _load,
    _open_scrap_bag,
    _scrap_cfg,
    _scrap_harness,
)

ROOT = Path(__file__).resolve().parents[1]
PHONE = "+353830867820"
KEY = "sEcReT+k3y/9"
# 2026-09-30 10:30 UTC = 11:30 in Dublin (IST, UTC+1).
START = datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc).timestamp()
END = START + 900.0


class Resp:
    def __init__(self, status: int, text: str = "Message queued") -> None:
        self.status_code = status
        self.text = text


class Log:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, event: str, **fields) -> None:
        with self.lock:
            self.rows.append({"event": event, **fields})

    def named(self, name: str) -> list[dict]:
        with self.lock:
            return [row for row in self.rows if row["event"] == name]

    def dump(self) -> str:
        with self.lock:
            return json.dumps(self.rows, default=str)


def _notifier(get, log=None, **kw) -> WhatsAppNotifier:
    kw.setdefault("sleep", lambda _s: None)
    return WhatsAppNotifier(PHONE, KEY, log=log, http_get=get, **kw)


def _scrap_intent(**extra) -> dict:
    row = {
        "start_ts": START,
        "end_ts": END,
        "sold_loser": True,
        "sold_leg": "dn",
        "sell_filled": 62.0,
        "sell_limit": 0.01,
        "sell_fill_px": 0.02,
        "sell_scrap_keep": 63.0,
        "slug": "btc-updown-15m-x",
    }
    row.update(extra)
    return row


class MessageFormatTests(unittest.TestCase):
    def test_scrap_message_matches_the_spec_example(self):
        text = scrap_message(
            _scrap_intent(), now=END - 192.0, bids={"up": 0.98, "dn": 0.01}
        )
        self.assertEqual(
            text,
            "Mintbot scrap: 11:30 bag | sold 62 DN @ 0.02 ($1.24) | 3m12s left | kept 63 | UP bid 0.98",
        )

    def test_scrap_uses_fill_then_limit_and_hides_missing_parts(self):
        text = scrap_message(
            _scrap_intent(sell_fill_px=0.015, sell_scrap_keep=None, sell_filled=50.0),
            now=END - 45.0,
        )
        self.assertEqual(text, "Mintbot scrap: 11:30 bag | sold 50 DN @ 0.015 ($0.75) | 45s left")
        text = scrap_message(_scrap_intent(sell_fill_px=None), now=END - 45.0)
        self.assertIn("@ 0.01 ($0.62)", text)
        self.assertIsNone(scrap_message(_scrap_intent(sell_filled=0.0), now=END))

    def test_partial_window_end_and_dump_messages(self):
        text = scrap_message(
            _scrap_intent(sold_loser=False, sell_filled=20.0), now=END + 3.0, partial=True
        )
        self.assertTrue(text.startswith("Mintbot scrap (partial, window ended): 11:30 bag | sold 20 DN"))
        self.assertIn("window ended", text)
        dump = dump_message(
            {
                "start_ts": START,
                "end_ts": END,
                "sell_dump_filled": 100.0,
                "sell_dump_fill_px": 0.31,
                "sell_dump_limit": 0.27,
                "sell_dump_leg": "up",
            },
            now=END - 192.0,
            bids={"up": 0.31, "dn": 0.66},
        )
        self.assertEqual(
            dump, "Mintbot DUMP: 11:30 bag | sold 100 UP @ 0.31 ($31.00) | 3m12s left | DN bid 0.66"
        )

    def test_start_label_is_dublin_time(self):
        self.assertEqual(bag_start_label(START), "11:30")
        winter = datetime(2026, 12, 1, 11, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(bag_start_label(winter), "11:30")
        self.assertEqual(bag_start_label(None), "?")
        self.assertEqual(bag_start_label(START, "No/Such_Zone"), "10:30 UTC")


class NoEnvTests(unittest.TestCase):
    def test_missing_env_is_a_silent_noop_with_one_startup_line(self):
        calls: list = []
        log = Log()
        notifier = WhatsAppNotifier.from_env({}, log=log, http_get=lambda *a, **k: calls.append(a))
        alerts = BagAlerts(notifier, log=log)
        alerts.startup({"dry_run": False})
        self.assertEqual(
            log.rows,
            [{"event": "notify_whatsapp_off", "reason": "missing_env",
              "missing": ["CALLMEBOT_PHONE", "CALLMEBOT_APIKEY"]}],
        )
        for _ in range(3):
            alerts.configure({"dry_run": False, "notify_scrap_whatsapp": True, "notify_dump_whatsapp": True})
            self.assertFalse(alerts.scrap_filled(_scrap_intent(), "cid", now=END - 10))
            self.assertFalse(alerts.dump_filled({"sell_dump_filled": 5, "end_ts": END}, "cid", now=END))
        self.assertFalse(notifier.send("hi", kind="scrap"))
        self.assertEqual(calls, [])
        self.assertIsNone(notifier._worker)
        self.assertEqual(len(log.rows), 1)
        only_key = WhatsAppNotifier.from_env({"CALLMEBOT_APIKEY": KEY})
        self.assertEqual(only_key.missing_env(), ["CALLMEBOT_PHONE"])
        self.assertFalse(only_key.enabled)

    def test_startup_line_when_on_hides_key_and_phone(self):
        log = Log()
        alerts = BagAlerts(_notifier(lambda *a, **k: Resp(200)), log=log)
        alerts.startup({"dry_run": False})
        self.assertEqual(
            log.rows,
            [{"event": "notify_whatsapp_on", "phone": "...7820", "scrap": True, "dump": False, "dry_run": False}],
        )
        self.assertNotIn(KEY, log.dump())
        self.assertNotIn(PHONE, log.dump())


class KnobTests(unittest.TestCase):
    def test_defaults_hot_reload_and_dry_run(self):
        sent: list = []
        alerts = BagAlerts(_notifier(lambda *a, **k: sent.append(k) or Resp(200)))
        alerts.configure({"dry_run": False})
        self.assertEqual((alerts.scrap, alerts.dump), (True, False))
        alerts.configure({"dry_run": False, "notify_scrap_whatsapp": False, "notify_dump_whatsapp": "true"})
        self.assertEqual((alerts.scrap, alerts.dump), (False, True))
        self.assertFalse(alerts.scrap_filled(_scrap_intent(), "c1", now=END - 10))
        alerts.configure({"dry_run": True})
        self.assertFalse(alerts.scrap_filled(_scrap_intent(), "c1", now=END - 10))
        alerts.configure({"dry_run": False})
        self.assertTrue(alerts.scrap_filled(_scrap_intent(), "c1", now=END - 10))
        self.assertFalse(alerts.scrap_filled(_scrap_intent(), "c1", now=END - 9))
        alerts.notifier.flush()
        self.assertEqual(len(sent), 1)

    def test_window_end_partial_only_just_after_the_end(self):
        alerts = BagAlerts(_notifier(lambda *a, **k: Resp(200)))
        alerts.configure({"dry_run": False})
        partial = _scrap_intent(sold_loser=False, sell_filled=20.0)
        self.assertFalse(alerts.scrap_filled(partial, "old", now=END + 3600, window_end=True))
        self.assertFalse(alerts.scrap_filled(_scrap_intent(), "done", now=END + 5, window_end=True))
        self.assertTrue(alerts.scrap_filled(partial, "fresh", now=END + 5, window_end=True))

    def test_load_strategy_accepts_missing_and_unknown_keys(self):
        tree = ast.parse((ROOT / "mintbot.py").read_text(encoding="utf-8"))
        defaults = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in node.targets
            ):
                defaults = ast.literal_eval(node.value)
        self.assertIs(defaults["notify_scrap_whatsapp"], True)
        self.assertIs(defaults["notify_dump_whatsapp"], False)
        example = json.loads((ROOT / "strategy_mint.example.json").read_text(encoding="utf-8"))
        folder = Path(tempfile.mkdtemp(prefix="strategy-"))
        for extra in ({}, {"notify_whatsapp_typo": 1}, {"notify_dump_whatsapp": True}):
            raw = {k: v for k, v in example.items() if not k.startswith("notify_")}
            raw.update(extra)
            path = folder / "strategy_mint.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            ns = _load("load_strategy", "validate_strategy", extras={"STRATEGY_FILE": path, "DEFAULTS": defaults, "validate_mint_gas": validate_mint_gas})
            cfg = ns["load_strategy"]()
            self.assertIs(cfg["notify_scrap_whatsapp"], True)
            self.assertIs(cfg["notify_dump_whatsapp"], bool(extra.get("notify_dump_whatsapp")))
            self.assertNotIn("notify_whatsapp_typo", cfg)


class DeliveryTests(unittest.TestCase):
    def test_sends_one_get_with_params_and_logs_sent(self):
        calls: list = []
        log = Log()

        def get(url, *, params, timeout):
            calls.append((url, params, timeout))
            return Resp(200)

        notifier = _notifier(get, log)
        self.assertTrue(notifier.send("hello", kind="scrap", meta={"condition_id": "c"}))
        self.assertTrue(notifier.flush())
        self.assertEqual(calls, [(CALLMEBOT_URL, {"phone": PHONE, "text": "hello", "apikey": KEY}, 8.0)])
        self.assertEqual(
            log.named("notify_sent"),
            [{"event": "notify_sent", "kind": "scrap", "status": 200, "attempts": 1, "condition_id": "c"}],
        )

    def test_one_retry_on_429_or_5xx_then_give_up_once(self):
        for script, event, attempts in (
            ([429, 200], "notify_sent", 2),
            ([503, 500], "notify_failed", 2),
            ([400], "notify_failed", 1),
            ([203], "notify_failed", 1),
        ):
            statuses = list(script)
            sleeps: list = []
            log = Log()

            def get(url, *, params, timeout):
                code = statuses.pop(0)
                body = "APIKey is invalid." if code == 203 else f"code {code}"
                return Resp(code, body)

            notifier = _notifier(get, log, sleep=sleeps.append)
            notifier.send("x", kind="scrap")
            notifier.flush()
            self.assertEqual(statuses, [], script)
            rows = log.named(event)
            self.assertEqual(len(rows), 1, script)
            self.assertEqual(rows[0]["attempts"], attempts)
            self.assertEqual(rows[0]["status"], script[-1])
            self.assertEqual(sleeps, [5.0] if attempts == 2 else [])
            self.assertEqual(len(log.rows), 1)

    def test_http_hang_never_blocks_send(self):
        release = threading.Event()
        started = threading.Event()
        log = Log()

        def get(url, *, params, timeout):
            started.set()
            release.wait(10)
            return Resp(200)

        notifier = _notifier(get, log, maxsize=50)
        t0 = time.monotonic()
        notifier.send("first", kind="scrap")
        started.wait(2)
        for i in range(120):
            self.assertTrue(notifier.send(f"m{i}", kind="scrap", meta={"n": i}))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)
        self.assertLessEqual(notifier._queue.qsize(), 50)
        dropped = log.named("notify_dropped")
        self.assertEqual(len(dropped), 70)
        self.assertEqual(dropped[0]["n"], 0)
        release.set()
        self.assertTrue(notifier.flush(5))
        self.assertEqual(len(log.named("notify_sent")), 51)

    def test_exceptions_are_contained(self):
        log = Log()
        boom = {"n": 0}

        def get(url, *, params, timeout):
            boom["n"] += 1
            if params["text"] == "bad":
                raise RuntimeError("socket exploded")
            return Resp(200)

        notifier = _notifier(get, log)
        notifier.send("bad", kind="scrap")
        notifier.send("good", kind="scrap")
        self.assertTrue(notifier.flush())
        self.assertEqual(len(log.named("notify_failed")), 1)
        self.assertEqual(len(log.named("notify_sent")), 1)

        def angry_log(*_a, **_k):
            raise ValueError("logger down")

        loud = _notifier(lambda *a, **k: Resp(200), angry_log)
        self.assertTrue(loud.send("x", kind="scrap"))
        self.assertTrue(loud.flush())
        alerts = BagAlerts(loud, log=angry_log)
        alerts.startup({"dry_run": False})
        alerts.configure(None)
        self.assertFalse(alerts.scrap_filled(None, "c", now=0.0))
        self.assertFalse(alerts.dump_filled({"sell_dump_filled": "nan?"}, "c", now=0.0))
        alerts.note_bids(None, object(), object())

    def test_mintbot_hook_swallows_errors(self):
        ns = _load("_whatsapp")
        ns["_whatsapp"]("scrap_filled", {}, "c", now=1.0)

        class Broken:
            def __getattr__(self, name):
                raise RuntimeError(name)

        ns["_BAG_ALERTS"] = Broken()
        ns["_whatsapp"]("scrap_filled", {}, "c", now=1.0)
        ns["_BAG_ALERTS"] = SimpleNamespace(scrap_filled=lambda *a, **k: 1 / 0)
        ns["_whatsapp"]("scrap_filled", {}, "c", now=1.0)


class RedactionTests(unittest.TestCase):
    def test_redact_raw_encoded_and_query_forms(self):
        url = f"{CALLMEBOT_URL}?phone=%2B353830867820&text=hi&apikey=sEcReT%2Bk3y%2F9"
        out = redact(f"GET {url} failed; key {KEY}; phone {PHONE}", KEY, PHONE)
        self.assertNotIn("sEcReT", out)
        self.assertNotIn("353830867820", out)
        self.assertEqual(out, "GET <url> failed; key ***; phone ***")
        self.assertEqual(redact("apikey=abc&x=1"), "apikey=***&x=1")

    def test_failure_logs_never_carry_the_key_or_url(self):
        log = Log()
        url = f"{CALLMEBOT_URL}?phone=%2B353830867820&text=hi&apikey=sEcReT%2Bk3y%2F9"

        def get(url_, *, params, timeout):
            raise ConnectionError(f"Max retries exceeded with url: {url} (key {KEY})")

        notifier = _notifier(get, log)
        notifier.send("hi", kind="scrap")
        notifier.flush()

        def invalid(url_, *, params, timeout):
            return Resp(203, f"Message to: {PHONE} Text to send: hi APIKey is invalid.")

        other = _notifier(invalid, log)
        other.send("hi", kind="scrap")
        other.flush()
        dumped = log.dump()
        self.assertEqual(len(log.named("notify_failed")), 2)
        for secret in ("sEcReT", "k3y", "353830867820", "api.callmebot.com/whatsapp.php?"):
            self.assertNotIn(secret, dumped)
        self.assertNotIn(KEY, repr(notifier.__dict__.get("phone")))


class SellLoopIntegrationTests(unittest.TestCase):
    def _alerts(self):
        sent: list = []
        notifier = _notifier(lambda url, *, params, timeout: sent.append(params["text"]) or Resp(200))
        return BagAlerts(notifier), sent

    def test_scrap_fill_sends_one_alert_with_the_fill_price(self):
        ns, _events, fak_calls, clock, _book = _scrap_harness()
        alerts, sent = self._alerts()
        ns["_BAG_ALERTS"] = alerts
        end = clock["now"] + 200.0
        intent = _open_scrap_bag(end, sell_loser_armed_at=clock["now"] - 10.0)
        cfg = _scrap_cfg(sell_floor=0.01, sell_scrap_max_ttm_s=0)
        for _ in range(3):
            state = {"intents": {"cid-wa": intent}}
            ns["remember_persisted_state"](state)
            ns["_manage_sells_locked"](cfg, state, object())
            clock["now"] += 1.0
        self.assertEqual(len(fak_calls), 1)
        self.assertTrue(alerts.notifier.flush())
        label = bag_start_label(end - 900.0)
        self.assertEqual(
            sent, [f"Mintbot scrap: {label} bag | sold 50 UP @ 0.015 ($0.75) | 3m20s left | DN bid 0.98"]
        )

    def test_dry_run_and_disabled_knob_send_nothing(self):
        for cfg_extra in ({"dry_run": True}, {"notify_scrap_whatsapp": False}):
            ns, _events, _fak, clock, _book = _scrap_harness()
            alerts, sent = self._alerts()
            ns["_BAG_ALERTS"] = alerts
            end = clock["now"] + 200.0
            intent = _open_scrap_bag(end, sell_loser_armed_at=clock["now"] - 10.0)
            cfg = _scrap_cfg(sell_floor=0.01, sell_scrap_max_ttm_s=0, **cfg_extra)
            state = {"intents": {"cid-wa": intent}}
            ns["remember_persisted_state"](state)
            ns["_manage_sells_locked"](cfg, state, object())
            self.assertTrue(intent.get("sold_loser"))
            alerts.notifier.flush()
            self.assertEqual(sent, [], cfg_extra)

    def test_dump_alert_is_opt_in(self):
        for on in (False, True):
            ns, _events, _fak, clock = _dump_harness(_fill_fak)
            alerts, sent = self._alerts()
            ns["_BAG_ALERTS"] = alerts
            end = clock["now"] + 200.0
            intent = _held_after_scrap(end, sell_dump_armed_at=clock["now"] - 2.0)
            state = {"intents": {"cid-dump": intent}}
            ns["remember_persisted_state"](state)
            ns["_manage_sells_locked"](_dump_cfg(notify_dump_whatsapp=on), state, object())
            self.assertTrue(intent.get("sold_dump"))
            alerts.notifier.flush()
            if on:
                self.assertEqual(len(sent), 1)
                self.assertTrue(sent[0].startswith("Mintbot DUMP: "))
                self.assertIn("sold 50 UP @ 0.51 ($25.50)", sent[0])
                self.assertIn("DN bid 0.49", sent[0])
            else:
                self.assertEqual(sent, [])

    def test_hung_notifier_does_not_slow_the_sell_tick(self):
        ns, _events, fak_calls, clock, _book = _scrap_harness()
        release = threading.Event()
        notifier = _notifier(lambda url, *, params, timeout: release.wait(10) or Resp(200))
        ns["_BAG_ALERTS"] = BagAlerts(notifier)
        end = clock["now"] + 200.0
        intent = _open_scrap_bag(end, sell_loser_armed_at=clock["now"] - 10.0)
        state = {"intents": {"cid-wa": intent}}
        ns["remember_persisted_state"](state)
        t0 = time.monotonic()
        ns["_manage_sells_locked"](_scrap_cfg(sell_floor=0.01, sell_scrap_max_ttm_s=0), state, object())
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(intent.get("sold_loser"))
        self.assertEqual(len(fak_calls), 1)
        release.set()
        self.assertTrue(notifier.flush())


if __name__ == "__main__":
    unittest.main()
