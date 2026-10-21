from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mintbot
from buy.fivemin import DEFAULTS, FiveMinIO, Runner, Strategy


def config(**extra):
    return {**DEFAULTS, "fivemin_enabled": True, "dry_run": False, **extra}


def market(cid="cid", start=1000, up="up", down="dn"):
    return SimpleNamespace(condition_id=cid, slug=f"btc-up-or-down-5m-{start}",
                           start_ts=start, end_ts=start + 300, up_token=up, dn_token=down,
                           active=True, closed=False, neg_risk=False)


class ClobWarmupTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(builder_config=None)
        self.client.get_tick_size.return_value = "0.001"
        self.client.get_neg_risk.return_value = False
        self.client._ClobClient__resolve_version = Mock(return_value=2)
        self.client.post_order.return_value = {
            "status": "matched", "takingAmount": "10", "makingAmount": "9",
        }
        self.events = []
        patches = [
            patch.object(mintbot, "_fak_market_cache", {}),
            patch.object(mintbot, "_fak_warmup_attempted", {}),
            patch.object(mintbot, "_fak_warmup_client_attempted", False),
            patch.object(mintbot, "_fak_warmup_warn_at", None),
            patch.object(mintbot, "_get_clob_client", return_value=self.client),
            patch.object(mintbot, "log_event", side_effect=lambda event, **kw: self.events.append((event, kw))),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_warmup_populates_cache_once_and_buy_skips_metadata_calls(self):
        mintbot._fak_warmup_sync(["up", "dn", "up"])
        self.assertEqual(mintbot._fak_market_cache, {"up": ("0.001", False), "dn": ("0.001", False)})
        self.assertEqual(self.client._ClobClient__resolve_version.call_count, 1)
        mintbot._fak_warmup_sync(["up", "dn"])
        self.assertEqual(self.client.get_tick_size.call_count, 2)
        self.client.get_tick_size.reset_mock()
        self.client.get_neg_risk.reset_mock()
        result = mintbot._fivemin_buy("up", 10, .9, False)
        self.assertEqual(result["shares"], 10)
        self.assertEqual(result["cost"], 9)
        self.client.get_tick_size.assert_not_called()
        self.client.get_neg_risk.assert_not_called()
        self.assertEqual(set(result["timing"]), {
            "client_init", "tick_size", "version", "neg_risk", "sign", "post", "retry",
        })
        self.assertTrue(all(result["timing"][step] >= 0 for step in
                            ("client_init", "tick_size", "version", "neg_risk", "sign", "post")))
        self.assertEqual(result["timing"]["retry"], {"count": 0, "ms": 0.0})

    def test_version_three_skips_neg_risk_and_bounds_recent_tokens(self):
        self.client._ClobClient__resolve_version.return_value = 3
        mintbot._fak_warmup_sync([str(i) for i in range(70)])
        self.assertEqual(len(mintbot._fak_market_cache), 64)
        self.assertEqual(len(mintbot._fak_warmup_attempted), 64)
        self.assertNotIn("0", mintbot._fak_market_cache)
        self.assertEqual(mintbot._fak_market_cache["69"], ("0.001", False))
        self.client.get_neg_risk.assert_not_called()

    def test_failure_warns_at_most_every_300_seconds_and_buy_falls_back(self):
        self.client.get_tick_size.side_effect = RuntimeError("cold REST failure")
        with patch.object(mintbot.time, "monotonic", return_value=100):
            mintbot._fak_warmup_sync(["up", "dn"])
            mintbot._fak_warmup_sync(["up"])
        self.assertEqual(self.client.get_tick_size.call_count, 2)
        self.assertEqual([event for event, _ in self.events], ["clob_warmup_fail"])
        self.assertEqual(self.events[0][1]["level"], "warning")
        with patch.object(mintbot.time, "monotonic", return_value=400):
            mintbot._fak_warmup_sync(["next"])
        self.assertEqual(len(self.events), 2)
        self.assertFalse(mintbot._fak_market_cache)
        self.client.get_tick_size.side_effect = None
        self.client.get_tick_size.reset_mock()
        bought, status = mintbot._fak_buy("up", 10, .9, False)
        self.assertEqual((bought, status), (10, "matched"))
        self.client.get_tick_size.assert_called_once_with("up")
        self.client.get_neg_risk.assert_called_once_with("up")

    def test_missing_client_warning_and_startup_only_once(self):
        mintbot._get_clob_client.return_value = None
        mintbot._fak_warmup_sync([])
        mintbot._fak_warmup_sync([])
        mintbot._get_clob_client.assert_called_once()
        self.assertEqual(self.events[0][0], "clob_warmup_fail")

    def test_retry_timing_includes_refresh_and_second_attempt(self):
        for first in (RuntimeError("not enough balance / allowance"),
                      {"error": "not enough balance / allowance"}):
            with self.subTest(first=first):
                self.client.post_order.side_effect = [first, {
                    "status": "matched", "takingAmount": "10", "makingAmount": "9",
                }]
                timing = {}
                with patch.object(mintbot, "_refresh_collateral_allowance") as refresh:
                    bought, status = mintbot._fak_buy("up", 10, .9, False, timing=timing)
                self.assertEqual((bought, status), (10, "matched"))
                refresh.assert_called_once_with(force=True)
                self.assertEqual(timing["retry"]["count"], 1)
                self.assertGreater(timing["retry"]["ms"], 0)

    def test_daemon_wrapper_returns_while_lookup_is_blocked(self):
        entered, release = threading.Event(), threading.Event()

        def lookup(_token):
            entered.set()
            release.wait(5)
            return "0.001"

        self.client.get_tick_size.side_effect = lookup
        real_thread = threading.Thread
        threads = []

        def make_thread(**kwargs):
            thread = real_thread(**kwargs)
            threads.append(thread)
            return thread

        with patch.object(mintbot.threading, "Thread", side_effect=make_thread):
            try:
                mintbot._fak_warmup_tokens(["up"])
                self.assertTrue(entered.wait(2))
                self.assertTrue(threads[0].daemon)
                self.assertTrue(threads[0].is_alive())
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)

    def test_dry_buy_skips_client(self):
        result = mintbot._fivemin_buy("up", 10, .9, True)
        mintbot._get_clob_client.assert_not_called()
        self.assertIn("timing", result)

    def test_entry_logs_fill_and_nofill_timing(self):
        for shares in (10, 0):
            with self.subTest(shares=shares):
                self.client.post_order.return_value = {
                    "status": "matched", "takingAmount": str(shares), "makingAmount": str(shares * .9),
                }
                io = FiveMinIO(buy=mintbot._fivemin_buy, sell=lambda *a: {},
                               reserves=lambda cfg: 0, redeem=lambda *a: None, save=lambda *a: None,
                               log=lambda event, **kw: self.events.append((event, kw)))
                strategy = Strategy(io, startup_ts=0)
                strategy.add_market(market(), now=1000)
                strategy.sample_cash(1200, 1000)
                row = strategy.state["markets"]["cid"]
                row["strike"] = 100
                self.events.clear()
                strategy._enter(config(), 1203, row, {"up": {"bid": .05}, "down": {"bid": .9}},
                                "up", "down", 1, 0, 3)
                fill = next(kw for event, kw in self.events if event == "fivemin_fill")
                self.assertIn("client_init", fill["timing"])
                if shares == 0:
                    nofill = next(kw for event, kw in self.events if event == "fivemin_signal_nofill")
                    self.assertEqual(nofill["timing"], fill["timing"])

    def test_runner_warms_only_changed_live_token_sets(self):
        cfg = config(fivemin_active_window_s=1000)
        warmup = Mock()
        io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda cfg: 0,
                       redeem=lambda *a: None, save=lambda *a: None, warmup=warmup)
        strategy = Strategy(io, startup_ts=0)
        books = Mock()
        books.usable.return_value = None
        rtds = SimpleNamespace(drain=lambda: [])
        runner = Runner(strategy, lambda: cfg, Mock(), Mock(), "funder", Mock(), Mock(),
                        rtds, books, clock=lambda: 1201)
        runner._markets = [market()]
        strategy.tick = Mock()
        runner.tick()
        runner.tick()
        warmup.assert_called_once_with(["up", "dn"])
        cfg["fivemin_dry_run"] = True
        runner._markets = [market(), market("next", 1300, "nextup", "nextdn")]
        runner.tick()
        self.assertEqual(warmup.call_count, 1)
        cfg["fivemin_dry_run"] = False
        runner.tick()
        self.assertEqual(warmup.call_count, 2)
        self.assertEqual(set(warmup.call_args.args[0]), {"up", "dn", "nextup", "nextdn"})
        runner.tick()
        self.assertEqual(warmup.call_count, 2)
