"""REST polling and persistence pacing tests; never import mintbot."""
import ast
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from buy.fivemin import DEFAULTS, FiveMinIO, Runner, Strategy
from buy.fivemin_books import RestBookFeed


BOOK = (0.1, 10.0, [{"price": "0.1", "size": "10"}], 0.2, 5.0, [], None)


def make_strategy(clock, state=None):
    saves = []
    io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda c: 0,
                   redeem=lambda *a: None, save=lambda s: saves.append(clock[0]),
                   clock=lambda: clock[0])
    return Strategy(io, state, startup_ts=clock[0]), saves


def row(cid="cid", start=1000, position=None):
    return dict(condition_id=cid, slug=f"btc-updown-5m-{start}", start=start, end=start + 300,
                up_token="up", dn_token="dn", entered=False, position=position)


def make_runner(clock, position=None):
    strategy, _ = make_strategy(clock)
    strategy.state["markets"]["cid"] = row(position=position)
    strategy._active.add("cid")
    strategy.tick = Mock()
    fetched = []
    books = RestBookFeed(lambda u, d: fetched.append(clock[0]) or (BOOK, BOOK), clock=lambda: clock[0])
    no_feed = SimpleNamespace(start=lambda: None, stop=lambda: None, drain=lambda: [])
    runner = Runner(strategy, lambda: DEFAULTS, None, None, "", None,
                    no_feed, no_feed, books, clock=lambda: clock[0])
    return runner, fetched


class RestBooksTests(unittest.TestCase):
    def test_usable_stale_crossed_missing_bid_and_full_depth(self):
        clock = [1000.0]
        books = RestBookFeed(lambda u, d: (BOOK, BOOK), clock=lambda: clock[0])
        books.start()
        books.set_tokens(["up", "dn"])
        books.set_min_size(0)
        books.set_stale_s(2)
        self.assertIsNone(books.usable("up"))
        books.poll(clock[0], "up", "dn")
        self.assertEqual(books.usable("up").bids, BOOK[2])
        clock[0] += 2.01
        self.assertIsNone(books.usable("up"))
        for bad in ((.3, 1, [], .2, 1, [], None), (None, 0, [], .2, 1, [], None)):
            books.fetch_pair = lambda u, d: (bad, BOOK)
            books.poll(clock[0], "up", "dn")
            self.assertIsNone(books.usable("up"))
        books.stop()
        self.assertIsNone(books.usable("dn"))

    def test_no_idle_poll_and_active_cadence(self):
        clock = [1100.0]
        runner, fetched = make_runner(clock)
        runner.tick()
        self.assertEqual(fetched, [])
        self.assertEqual(runner.sleep_s(), 1.0)
        clock[0] = 1169
        self.assertEqual(runner.sleep_s(), .2)
        for now in (1170, 1170.1, 1170.2, 1170.3, 1170.4):
            clock[0] = now
            runner.tick()
        self.assertEqual(len(fetched), 3)
        self.assertEqual(runner.sleep_s(), .2)

    def test_open_position_polls_outside_and_after_window(self):
        clock = [1050.0]
        runner, fetched = make_runner(clock, {"settled": False})
        runner.tick()
        clock[0] = 1400
        runner.tick()
        self.assertEqual(fetched, [1050, 1400])
        self.assertEqual(runner.sleep_s(), .2)

    def test_300_second_simulation_fetch_count(self):
        clock = [1000.0]
        runner, fetched = make_runner(clock)
        for tick in range(3001):
            clock[0] = 1000 + tick / 10
            runner.tick()
        self.assertTrue(all(now >= 1170 for now in fetched))
        self.assertLessEqual(abs(len(fetched) - 650), 5)

    def test_discovery_list_reused_until_refresh(self):
        clock = [1000.0]
        runner, _ = make_runner(clock)
        runner.strategy.add_market = Mock()
        runner._markets = [SimpleNamespace(condition_id="new", active=True, closed=False, neg_risk=False)]
        runner.tick()
        runner.tick()
        self.assertEqual(runner.strategy.add_market.call_count, 1)


class StatePacingTests(unittest.TestCase):
    def test_save_debounce_pending_tick_and_force(self):
        clock = [1000.0]
        strategy, saves = make_strategy(clock)
        strategy.state["day"] = "1970-01-01"
        for _ in range(100):
            strategy.save()
        self.assertEqual(saves, [1000])
        clock[0] = 1000.5
        strategy.save(force=True)
        self.assertEqual(saves, [1000, 1000.5])
        strategy.save()
        clock[0] = 1001.5
        strategy.tick(DEFAULTS, clock[0], None, {})
        self.assertEqual(saves, [1000, 1000.5, 1001.5])
        self.assertFalse(strategy._save_dirty)

    def test_prune_ten_minutes_and_preserve_unsettled(self):
        clock = [1000.0]
        strategy, _ = make_strategy(clock)
        strategy.state["markets"] = {
            "empty": row("empty", start=0),
            "open": row("open", start=0, position={"settled": False}),
            "done": row("done", start=0, position={"settled": True, "shares": 0}),
            "recent": row("recent", start=200),
        }
        strategy._prune(901)
        self.assertEqual(set(strategy.state["markets"]), {"open", "recent"})

    def test_startup_skip_pruned_on_init(self):
        clock = [1000.0]
        state = {"markets": {"old": {**row("old", start=0), "skip_reason": "startup"},
                             "recent": {**row("recent", start=200), "skip_reason": "startup"},
                             "open": {**row("open", start=0, position={"settled": False}), "skip_reason": "startup"}}}
        strategy, _ = make_strategy(clock, state)
        self.assertEqual(set(strategy.state["markets"]), {"recent", "open"})

    def test_stop_file_checked_once_per_second_and_shutdown_immediate(self):
        source = Path(__file__).resolve().parents[1] / "mintbot.py"
        tree = ast.parse(source.read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        nodes = [n for n in main.body if (isinstance(n, ast.FunctionDef) and n.name == "should_stop")
                 or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "stop_check" for t in n.targets))]
        clock = [0.0]
        exists = Mock(return_value=False)
        ns = dict(_shutdown=False, STOP_FILE=SimpleNamespace(exists=exists),
                  time=SimpleNamespace(monotonic=lambda: clock[0]))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
        for tick in range(100):
            clock[0] = tick / 100
            self.assertFalse(ns["should_stop"]())
        self.assertEqual(exists.call_count, 1)
        clock[0] = 1
        exists.return_value = True
        self.assertTrue(ns["should_stop"]())
        self.assertEqual(exists.call_count, 2)
        ns["_shutdown"] = True
        self.assertTrue(ns["should_stop"]())
        self.assertEqual(exists.call_count, 2)
