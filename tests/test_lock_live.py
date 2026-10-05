"""Legacy ledger separation and reset behavior."""
import tempfile
import unittest
from pathlib import Path
from buy.lock_state import (ledger_path, load_ledger, reset_live_ledger, save_ledger,
                            load_windows, save_windows, strike_for_position)
from buy.lock_bookws import ClobBookFeed, apply_book_message, plan_subscriptions
from buy.lock_ws import connect_kwargs, heartbeat_kind

class LedgerTests(unittest.TestCase):

    def test_live_and_paper_are_different_files(self):
        root = Path('/tmp/lockbot-ledger-names')
        self.assertEqual(ledger_path(root, True).name, 'positions_lockbot.json')
        self.assertEqual(ledger_path(root, False).name, 'positions_lockbot_live.json')

    def test_reset_clears_live_and_leaves_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = ledger_path(root, True)
            save_ledger(paper, {'positions': {'keep': {'pnl': -15.0}}, 'intents': {}, 'redeems': {}, 'ledger': 'paper'})
            live = ledger_path(root, False)
            save_ledger(live, {'positions': {'gone': {'pnl': -1.0}}, 'intents': {'a': 1}, 'redeems': {}, 'ledger': 'live'})
            reset_live_ledger(root, now=10.0)
            self.assertEqual(load_ledger(paper, 'paper')['positions']['keep']['pnl'], -15.0)
            cleared = load_ledger(live, 'live')
            self.assertEqual(cleared['positions'], {})
            self.assertEqual(cleared['intents'], {})
            self.assertEqual(cleared['reset_ts'], 10.0)
            self.assertEqual(cleared['ledger'], 'live')


class BookFeedTests(unittest.TestCase):

    def test_plan_drops_expired_tokens_and_keeps_order(self):
        drop, add = plan_subscriptions({'a', 'b', 'c'}, ['b', 'd'])
        self.assertEqual(drop, ['a', 'c'])
        self.assertEqual(add, ['d'])

    def test_send_does_not_touch_a_missing_socket(self):
        feed = ClobBookFeed()
        self.assertIsNone(feed._ws)
        self.assertFalse(feed._send({'type': 'market', 'assets_ids': ['tok']}))
        feed._safe_close()
        self.assertIsNone(feed._ws)

    def test_deltas_stay_sorted_without_a_full_replace(self):
        feed = ClobBookFeed(clock=lambda: 10.0)
        feed.handle_message({'event_type': 'book', 'asset_id': 'UP', 'asks': [{'price': '0.40', 'size': '10'}], 'bids': [{'price': '0.39', 'size': '4'}]})
        feed.handle_message({'event_type': 'price_change', 'price_changes': [{'asset_id': 'UP', 'side': 'SELL', 'price': '0.41', 'size': '5'}]})
        book = feed.book('UP')
        self.assertEqual(book['asks'], [(0.4, 10.0), (0.41, 5.0)])
        feed.handle_message({'event_type': 'price_change', 'price_changes': [{'asset_id': 'UP', 'side': 'SELL', 'price': '0.40', 'size': '0'}]})
        self.assertEqual(feed.book('UP')['asks'], [(0.41, 5.0)])

    def test_apply_book_message_still_returns_lists(self):
        books = {}
        apply_book_message(books, {'event_type': 'book', 'asset_id': 'UP', 'asks': [{'price': '0.40', 'size': '10'}], 'bids': []}, now=1.0)
        self.assertEqual(books['UP']['asks'], [(0.4, 10.0)])


class FeedCostTests(unittest.TestCase):

    def test_sockets_skip_utf8_validation(self):
        self.assertEqual(connect_kwargs(), {'skip_utf8_validation': True})
        self.assertEqual(heartbeat_kind('PING'), 'PING')
        self.assertEqual(heartbeat_kind(b' pong '), 'PONG')
        self.assertEqual(heartbeat_kind('{'), '')

    def test_same_book_tokens_do_not_mark_the_socket_dirty(self):
        feed = ClobBookFeed()
        feed.set_tokens(['up', 'dn'])
        self.assertTrue(feed._subs_dirty.is_set())
        feed._subs_dirty.clear()
        feed.set_tokens(['up', 'dn'])
        self.assertFalse(feed._subs_dirty.is_set())
        feed.set_tokens(['dn', 'up'])
        self.assertTrue(feed._subs_dirty.is_set())


class WindowTests(unittest.TestCase):

    def test_strikes_reload_and_cover_a_position_that_missed_the_latch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        path = root / 'lockbot_windows.json'
        now = 1000000.0
        save_windows(path, {'btc-updown-5m-999700': {'strike': 64010.0, 'start_ts': 999700.0, 'end_ts': 1000000.0, 'latched_at': 999700.0, 'source': 'rtds'}, 'btc-updown-5m-1': {'strike': 1.0, 'start_ts': 1.0, 'end_ts': 301.0, 'latched_at': 1.0}}, now)
        loaded = load_windows(path, now)
        self.assertEqual(set(loaded), {'btc-updown-5m-999700'})
        self.assertEqual(strike_for_position({'slug': 'btc-updown-5m-999700'}, {'btc-updown-5m-999700': 64010.0}), 64010.0)
        self.assertEqual(strike_for_position({'slug': 'btc-updown-5m-999700', 'strike': 1.0}, {}), 1.0)
        self.assertIsNone(strike_for_position({'slug': 'missing'}, {}))
