"""Idle operation, config reload, and retained settlement reporting."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import lockbot
from buy.lock_config import apply_defaults, validate_config
from buy.lock_report import format_summary, summarize
from buy.lock_state import ledger_path, save_ledger


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.config.write_text('{}')
        patcher = patch.multiple(lockbot, ROOT=self.root, log_event=Mock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = lockbot.LockBot(apply_defaults({}), config_path=self.config)
        self.addCleanup(self.bot.close)

    def reload_config(self, raw):
        self.config.write_text(json.dumps(raw))
        stamp = self.bot.config_mtime + 2
        os.utime(self.config, (stamp, stamp))
        self.bot.reload()

    def test_empty_paper_and_live_modes_idle_without_feeds_or_orders(self):
        for dry in (True, False):
            self.reload_config({'enabled': True, 'dry_run': dry, 'strategy1_enabled': True,
                                'strategy2_enabled': True, 'strategy3_enabled': True,
                                'h2h_enabled': True})
            with patch.object(self.bot.book_feed, 'start') as start, \
                 patch('lockbot.RtdsTwapFeed') as oracle, \
                 patch('lockbot.fetch_market') as fetch:
                self.assertEqual(self.bot.tick(), 0)
                start.assert_not_called()
                oracle.assert_not_called()
                fetch.assert_not_called()
            self.assertEqual(self.bot.state['ledger'], 'paper' if dry else 'live')
            for name in ('_act', '_activate_live', '_consider_s3_fill', '_on_wallet_fill', '_poster', 'client'):
                self.assertFalse(hasattr(self.bot, name), name)
            self.assertFalse(any(k.startswith(('strategy', 'h2h_', 's1_', 's2_')) for k in self.bot.cfg))

    def test_hot_reload_preserves_ledgers_and_uncertain_records(self):
        paper = {'positions': {'paper': {'shares': 1}}, 'uncertain_orders': {'old': {'notional': 2}}}
        live = {'positions': {'live': {'shares': 2}}, 'uncertain_orders': {'pending': {'notional': 3}}}
        save_ledger(ledger_path(self.root, True), paper)
        save_ledger(ledger_path(self.root, False), live)
        self.reload_config({'dry_run': False})
        self.assertEqual(self.bot.state['positions'], live['positions'])
        self.assertEqual(self.bot.state['uncertain_orders'], live['uncertain_orders'])
        self.reload_config({'dry_run': True})
        self.assertEqual(self.bot.state['positions'], paper['positions'])
        self.assertEqual(self.bot.state['uncertain_orders'], paper['uncertain_orders'])

    def test_invalid_reload_keeps_last_valid_config(self):
        original = self.bot.cfg
        self.reload_config({'poll_s': 0})
        self.assertIs(self.bot.cfg, original)
        self.config.write_text('[]')
        with self.assertRaises(ValueError):
            lockbot.read_config(self.config)

    def test_oracle_feed_only_for_unsettled_positions(self):
        pos = {'slug': 'btc-updown-5m-1791170100', 'shares': 1}
        self.bot.state['positions'] = {'old': pos}
        with patch('lockbot.RtdsTwapFeed') as constructor:
            self.bot.ensure_feeds()
            constructor.assert_called_once()
            feed = constructor.return_value
            feed.start.assert_called_once()
            pos['settled_ts'] = 1791170401
            self.bot.ensure_feeds()
            feed.stop.assert_called_once()
            self.assertEqual(self.bot.feeds, {})

    def test_book_logging_hot_reload_exercises_discovery_and_subscriptions(self):
        self.reload_config({'book_log_enabled': True})
        with patch.object(self.bot.book_feed, 'start') as start, \
             patch.object(self.bot, 'refresh_markets') as markets, \
             patch.object(self.bot, 'subscribe_books') as subscribe:
            self.bot.tick(1000)
            start.assert_called_once()
            markets.assert_called_once_with(1000)
            subscribe.assert_called_once_with(1000)
        self.assertIsNotNone(self.bot.book_feed.touch_hook)
        self.reload_config({'book_log_enabled': False})
        self.assertIsNone(self.bot.book_feed.touch_hook)
        with patch.object(self.bot, 'refresh_markets') as markets:
            self.bot.tick(1001)
            markets.assert_not_called()

    def test_main_starts_and_stops_cleanly_in_isolated_root(self):
        with patch.multiple(lockbot, LOCK_FILE=self.root / 'lock', STOP_FILE=self.root / 'stop'), \
             patch.object(lockbot._stop, 'wait', side_effect=lambda _: lockbot._stop.set()), \
             patch('lockbot.signal.signal'):
            lockbot._stop.clear()
            try:
                self.assertEqual(lockbot.main(['--config', str(self.config)]), 0)
            finally:
                lockbot._stop.clear()
        calls = lockbot.log_event.call_args_list
        self.assertTrue(any(c.args[0] == 'startup' and c.kwargs['mode'] == 'idle' for c in calls))
        self.assertTrue(any(c.args[0] == 'shutdown' for c in calls))


class ConfigAndReportTests(unittest.TestCase):
    def test_safe_defaults_and_invalid_logger_settings(self):
        cfg = apply_defaults({})
        self.assertTrue(cfg['dry_run'])
        self.assertFalse(cfg['enabled'])
        self.assertFalse(cfg['book_log_enabled'])
        validate_config(cfg)
        for key, value in [('poll_s', float('nan')), ('book_log_levels', .5),
                           ('book_log_path', ''), ('book_log_max_bytes', -1)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_config(apply_defaults({key: value}))

    def test_legacy_settlement_report_deduplicates_strategy_and_side(self):
        rows = [dict(event='settlement', slug='btc', strategy=s, side=side,
                     pnl=pnl, won=pnl > 0, asset='btc', lane='btc_5m')
                for s, side, pnl in [('s1', 'up', 2), ('s2', 'down', -1), ('s3', 'up', 3)]]
        report = summarize(rows + [rows[0], {'event': 'wallet_fill'}])
        self.assertEqual(report['markets'], 3)
        self.assertEqual(report['pnl'], 4)
        self.assertEqual(set(report['strategies']), {'s1', 's2', 's3'})
        self.assertIn('s3: settled 1', format_summary(report))
        self.assertEqual(summarize([])['markets'], 0)
