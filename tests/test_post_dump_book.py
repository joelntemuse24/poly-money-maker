"""Post-dump book journal: observation only, no extra read on the order path."""

from __future__ import annotations

import unittest

from buy.mint_sell import (
    POST_DUMP_BOOK_INTERVAL_S,
    post_dump_book_due,
    post_dump_book_sample,
)
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap
from test_mint_only_ops import _assign
from test_mint_reclaim import _Loop, _row


def _dumped(end: float) -> dict:
    return _held_after_scrap(
        end,
        sold_dump=True,
        sold_winner=True,
        sell_dump_leg="dn",
    )


def _events(loop, name: str) -> list:
    return [row for row in loop.events if row["event"] == name]


class PostDumpBookDecisionTests(unittest.TestCase):
    def test_interval_and_sample_actions(self):
        self.assertEqual(POST_DUMP_BOOK_INTERVAL_S, 0.25)
        self.assertTrue(post_dump_book_due(10.0, None))
        self.assertFalse(post_dump_book_due(10.1, 10.0))
        self.assertTrue(post_dump_book_due(10.25, 10.0))
        self.assertEqual(
            post_dump_book_sample(
                enabled=False, sold_dump=True, now_s=10.0, end_ts=20.0,
                last_logged_at=None, book_ts=10.0,
            ),
            "skip",
        )
        self.assertEqual(
            post_dump_book_sample(
                enabled=True, sold_dump=False, now_s=10.0, end_ts=20.0,
                last_logged_at=None, book_ts=10.0,
            ),
            "skip",
        )
        self.assertEqual(
            post_dump_book_sample(
                enabled=True, sold_dump=True, now_s=20.0, end_ts=20.0,
                last_logged_at=None, book_ts=20.0,
            ),
            "skip",
        )
        self.assertEqual(
            post_dump_book_sample(
                enabled=True, sold_dump=True, now_s=10.0, end_ts=20.0,
                last_logged_at=None, book_ts=10.0,
            ),
            "emit",
        )
        self.assertEqual(
            post_dump_book_sample(
                enabled=True, sold_dump=True, now_s=10.25, end_ts=20.0,
                last_logged_at=10.0, book_ts=10.0,
            ),
            "fetch",
        )
        self.assertEqual(
            post_dump_book_sample(
                enabled=True, sold_dump=True, now_s=10.25, end_ts=20.0,
                last_logged_at=10.0, book_ts=10.2,
            ),
            "emit",
        )

    def test_default_is_on(self):
        self.assertIs(_assign("DEFAULTS")["post_dump_log_enabled"], True)


class PostDumpBookLoopTests(unittest.TestCase):
    def _book(self):
        return {
            "up": _row(0.62, 0.63),
            "dn": _row(0.36, 0.37),
        }

    def test_logs_the_tick_book_then_waits_a_quarter_second(self):
        loop = _Loop()
        fetches = {"n": 0}
        real = loop.ns["_fetch_books"]

        def fetch_books(up, dn, min_size):
            fetches["n"] += 1
            return real(up, dn, min_size)

        loop.ns["_fetch_books"] = fetch_books
        loop.book = self._book()
        now = loop.clock["now"]
        intent = _dumped(now + 200.0)
        cfg = _dump_cfg(sell_enabled=True)
        loop.tick(cfg, intent)
        self.assertEqual(fetches["n"], 1)
        logged = _events(loop, "post_dump_book")
        self.assertEqual(len(logged), 1)
        row = logged[0]
        self.assertEqual(row["leg_dumped"], "dn")
        self.assertAlmostEqual(row["up_bid"], 0.62)
        self.assertAlmostEqual(row["up_ask"], 0.63)
        self.assertAlmostEqual(row["dn_bid"], 0.36)
        self.assertAlmostEqual(row["dn_ask"], 0.37)
        self.assertAlmostEqual(row["ttm_s"], 200.0)
        self.assertEqual(row["up_bids"], [[0.62, 500.0]])
        self.assertEqual(row["up_asks"], [[0.63, 500.0]])
        self.assertEqual(row["dn_bids"], [[0.36, 500.0]])
        self.assertEqual(row["dn_asks"], [[0.37, 500.0]])
        self.assertEqual(row["up_ask_near"], 500.0)
        self.assertEqual(row["dn_ask_near"], 500.0)
        loop.clock["now"] = now + 0.1
        loop.tick(cfg, intent)
        self.assertEqual(len(_events(loop, "post_dump_book")), 1)
        self.assertEqual(fetches["n"], 2)
        loop.clock["now"] = now + 0.25
        loop.book = {
            "up": _row(0.70, 0.71),
            "dn": _row(0.28, 0.29),
        }
        loop.tick(cfg, intent)
        self.assertEqual(fetches["n"], 3)
        again = _events(loop, "post_dump_book")
        self.assertEqual(len(again), 2)
        self.assertAlmostEqual(again[-1]["up_ask"], 0.71)
        self.assertAlmostEqual(again[-1]["dn_bid"], 0.28)
        self.assertEqual(loop.sells, [])
        self.assertEqual(loop.buys, [])

    def test_flag_off_and_no_dump_do_not_log(self):
        loop = _Loop()
        loop.book = self._book()
        now = loop.clock["now"]
        dumped = _dumped(now + 200.0)
        loop.tick(_dump_cfg(post_dump_log_enabled=False), dumped)
        self.assertEqual(_events(loop, "post_dump_book"), [])
        quiet = _Loop()
        quiet.book = self._book()
        held = _held_after_scrap(quiet.clock["now"] + 200.0)
        quiet.tick(_dump_cfg(sell_dump_enabled=False), held)
        self.assertEqual(_events(quiet, "post_dump_book"), [])

    def test_dump_fire_still_sells_and_does_not_fetch_twice(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        fetches = {"n": 0}
        real = ns["_fetch_books"]

        def fetch_books(up, dn, min_size):
            fetches["n"] += 1
            return real(up, dn, min_size)

        ns["_fetch_books"] = fetch_books
        intent = _held_after_scrap(
            clock["now"] + 200.0,
            sell_dump_armed_at=clock["now"] - 2.0,
        )
        state = {"intents": {"cid-dump": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](_dump_cfg(), state, object())
        names = [row["event"] for row in events]
        self.assertIn("sell_dump_done", names)
        self.assertIn("post_dump_book", names)
        self.assertLess(names.index("sell_dump_done"), names.index("post_dump_book"))
        self.assertEqual(fetches["n"], 1)
        self.assertTrue(fak_calls)
        self.assertEqual(intent["sell_dump_leg"], "up")
        logged = [row for row in events if row["event"] == "post_dump_book"][-1]
        self.assertEqual(logged["leg_dumped"], "up")
        self.assertAlmostEqual(logged["ttm_s"], 200.0)

    def test_sampler_reuses_a_fresh_read_and_fetches_when_stale(self):
        loop = _Loop()
        fetches = {"n": 0}

        def fetch_books(_up, _dn, _min):
            fetches["n"] += 1
            return _row(0.40, 0.41), _row(0.58, 0.59)

        loop.ns["_fetch_books"] = fetch_books
        loop.book = self._book()
        now = loop.clock["now"]
        intent = _dumped(now + 50.0)
        loop.tick(_dump_cfg(), intent)
        self.assertEqual(fetches["n"], 1)
        box = loop.ns["_post_dump_box"]()
        snap = box["snaps"]["cid-reclaim"]
        snap["logged_at"] = now - 0.25
        snap["book_ts"] = now
        snap["up_ask"] = 0.22
        before = fetches["n"]
        loop.ns["_post_dump_book_pass"](now)
        self.assertEqual(fetches["n"], before)
        self.assertAlmostEqual(_events(loop, "post_dump_book")[-1]["up_ask"], 0.22)

        snap["logged_at"] = now - 0.25
        snap["book_ts"] = now - 0.25
        loop.ns["_post_dump_book_pass"](now)
        self.assertEqual(fetches["n"], before + 1)
        fresh = _events(loop, "post_dump_book")[-1]
        self.assertAlmostEqual(fresh["up_bid"], 0.40)
        self.assertAlmostEqual(fresh["up_ask"], 0.41)
        self.assertAlmostEqual(fresh["dn_bid"], 0.58)
        self.assertAlmostEqual(fresh["dn_ask"], 0.59)
        self.assertEqual(fresh["leg_dumped"], "dn")
        self.assertEqual(fresh["up_bids"], [[0.40, 500.0]])
        self.assertEqual(fresh["dn_asks"], [[0.59, 500.0]])
        self.assertEqual(fresh["up_ask_near"], 500.0)

        snap = box["snaps"]["cid-reclaim"]
        snap["logged_at"] = now - 1.0
        snap["book_ts"] = now - 1.0
        snap["end_ts"] = now
        loop.ns["_post_dump_book_pass"](now)
        self.assertEqual(fetches["n"], before + 1)
        self.assertNotIn("cid-reclaim", box["snaps"])

    def test_sampler_does_not_fetch_when_the_flag_is_off(self):
        loop = _Loop()
        fetches = {"n": 0}
        real = loop.ns["_fetch_books"]

        def fetch_books(*args, **kwargs):
            fetches["n"] += 1
            return real(*args, **kwargs)

        loop.ns["_fetch_books"] = fetch_books
        loop.book = self._book()
        now = loop.clock["now"]
        cfg = _dump_cfg()
        loop.tick(cfg, _dumped(now + 50.0))
        self.assertEqual(fetches["n"], 1)
        cfg["post_dump_log_enabled"] = False
        box = loop.ns["_post_dump_box"]()
        snap = box["snaps"]["cid-reclaim"]
        snap["logged_at"] = now - 1.0
        snap["book_ts"] = now - 1.0
        loop.ns["_post_dump_book_pass"](now + 1.0)
        self.assertEqual(fetches["n"], 1)
        self.assertEqual(len(_events(loop, "post_dump_book")), 1)

    def test_multi_level_depth_uses_the_tick_book(self):
        loop = _Loop()
        fetches = {"n": 0}
        real = loop.ns["_fetch_books"]

        def fetch_books(up, dn, min_size):
            fetches["n"] += 1
            return real(up, dn, min_size)

        loop.ns["_fetch_books"] = fetch_books

        def levels(pairs):
            return [{"price": f"{px:.4f}", "size": f"{sz:.4f}"} for px, sz in pairs]

        up_bids = levels([
            (0.10, 1), (0.62, 10), (0.61, 20), (0.60, 30),
            (0.59, 40), (0.58, 50), (0.90, 0),
        ])
        up_asks = levels([
            (0.70, 5), (0.63, 11), (0.64, 12), (0.65, 13),
            (0.66, 14), (0.80, 99),
        ])
        dn_bids = levels([(0.36, 7), (0.35, 8), (0.34, 9)])
        dn_asks = levels([(0.40, 4), (0.37, 6), (0.38, 3), (0.39, 2)])
        loop.book = {
            "up": (0.62, 10.0, up_bids, 0.63, 11.0, up_asks, None),
            "dn": (0.36, 7.0, dn_bids, 0.37, 6.0, dn_asks, None),
        }
        now = loop.clock["now"]
        loop.tick(_dump_cfg(), _dumped(now + 80.0))
        self.assertEqual(fetches["n"], 1)
        row = _events(loop, "post_dump_book")[-1]
        self.assertEqual(
            row["up_bids"],
            [[0.62, 10.0], [0.61, 20.0], [0.60, 30.0], [0.59, 40.0], [0.58, 50.0]],
        )
        self.assertEqual(
            row["up_asks"],
            [[0.63, 11.0], [0.64, 12.0], [0.65, 13.0], [0.66, 14.0], [0.70, 5.0]],
        )
        self.assertEqual(row["up_ask_near"], 50.0)
        self.assertEqual(row["dn_bids"], [[0.36, 7.0], [0.35, 8.0], [0.34, 9.0]])
        self.assertEqual(
            row["dn_asks"],
            [[0.37, 6.0], [0.38, 3.0], [0.39, 2.0], [0.40, 4.0]],
        )
        self.assertEqual(row["dn_ask_near"], 15.0)
        self.assertEqual(loop.buys, [])


if __name__ == "__main__":
    unittest.main()
