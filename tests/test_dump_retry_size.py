"""Dump retries post the floored remainder, including after a balance reject."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from buy.mint_sell import floor_sell_shares, record_fill_px, sell_remaining_shares
from test_mint_cpu import (
    _dump_cfg,
    _dump_harness,
    _held_after_scrap,
    _open_scrap_bag,
    _scrap_cfg,
    _scrap_harness,
    _sell_runtime,
)
from test_mint_only_ops import _fn


class FloorSellSharesTests(unittest.TestCase):
    def test_floors_to_exchange_share_precision(self):
        self.assertEqual(floor_sell_shares(73.53), 73.53)
        self.assertEqual(floor_sell_shares("73.529999999"), 73.52)
        self.assertEqual(sell_remaining_shares(100, 26.47), 73.53)
        self.assertEqual(sell_remaining_shares(100, 26.47, 50), 50.0)
        self.assertEqual(sell_remaining_shares(100, 0, 73.53), 73.53)


class DumpRetrySizeTests(unittest.TestCase):
    def _run(self, outcomes, *, balance=None, books=None, retries=0):
        sizes: list[float] = []
        reads: list[str] = []
        events: list[tuple] = []
        book_iter = iter(books or [(0.15, 80.0, [{"price": "0.15", "size": "80"}])])
        outcome_iter = iter(outcomes)

        def ladder(*args, **_kwargs):
            sizes.append(float(args[1]))
            return next(outcome_iter)

        def read_balance(token_id):
            reads.append(str(token_id))
            return balance

        fn = _fn(
            "_run_dump_fak_with_refire",
            {
                "_run_fak_ladder": ladder,
                "_fetch_book": lambda *_a, **_k: next(book_iter),
                "_io_unlocked": nullcontext,
                "_read_sell_balance": read_balance,
                "dump_fast_retry_eligible": lambda **_kwargs: False,
                "log_event": lambda event, **payload: events.append((event, payload)),
                "time": SimpleNamespace(time=lambda: 1_000.0),
            },
        )
        sold, status, _px, attempts, _bid = fn(
            token_id="up-tok",
            size=100.0,
            initial_bid=0.31,
            initial_bids=[{"price": "0.31", "size": "100"}],
            held="up",
            slug="btc-updown-15m-1345",
            condition_id="cid",
            ttm_s=120.0,
            floor=0.01,
            min_bid_size=1.0,
            retries=retries,
            ladder_step=0.04,
            ladder_rungs=4,
            dry_run=False,
            tol=0.01,
            end_ts=10_000_000_000.0,
        )
        return sizes, reads, events, sold, status, attempts

    def test_partial_fill_retries_the_floored_remainder(self):
        sizes, reads, _events, sold, status, attempts = self._run(
            [
                (26.47, "matched", 0.31),
                (73.53, "matched", 0.15),
            ]
        )
        self.assertEqual(sizes, [100.0, 73.53])
        self.assertEqual(reads, [])
        self.assertAlmostEqual(sold, 100.0)
        self.assertEqual(status, "matched")
        self.assertEqual(attempts, 2)

    def test_balance_reject_posts_the_wallet_and_keeps_going(self):
        sizes, reads, events, sold, status, attempts = self._run(
            [
                (0.0, "error:not enough balance / allowance", 0.31),
                (73.53, "matched", 0.15),
            ],
            balance=73.53,
        )
        self.assertEqual(sizes, [100.0, 73.53])
        self.assertEqual(reads, ["up-tok"])
        # 26.47 left the wallet with no price; 73.53 is the priced retry.
        self.assertAlmostEqual(sold, 100.0)
        self.assertEqual(status, "matched")
        self.assertEqual(attempts, 2)
        self.assertFalse(
            any(event == "sell_dump_fast_refire_stop" for event, _payload in events)
        )

    def test_unchanged_balance_does_not_spin(self):
        sizes, reads, events, sold, _status, attempts = self._run(
            [(0.0, "error:not enough balance / allowance", 0.31)],
            balance=100.0,
        )
        self.assertEqual(sizes, [100.0])
        self.assertEqual(reads, ["up-tok"])
        self.assertEqual(attempts, 1)
        self.assertAlmostEqual(sold, 0.0)
        self.assertEqual(events[-1][1]["reason"], "balance_unchanged")


class DumpAndLoserLogTotalTests(unittest.TestCase):
    def test_dump_done_sums_chunks_and_weights_real_fills(self):
        ns, events, fak_calls, clock = _dump_harness(lambda *_a, **_k: (0.0, "idle"))
        calls = {"n": 0}

        def priced(token_id, size, price, dry_run, capture=None):
            calls["n"] += 1
            fak_calls.append(
                {"token_id": token_id, "size": float(size), "price": float(price)}
            )
            if calls["n"] == 1:
                sold, px, status = 26.47, 0.31, "matched"
            elif calls["n"] == 2:
                return 0.0, "error:timeout"
            else:
                sold, px, status = float(size), 0.15, "matched"
            if capture is not None and sold > 0:
                capture.append(
                    {
                        "status": status,
                        "makingAmount": str(sold),
                        "takingAmount": str(round(sold * px, 6)),
                    }
                )
            return sold, status

        ns["_fak_sell"] = priced
        end = clock["now"] + 200.0
        intent = _held_after_scrap(
            end, shares=100.0, sell_dump_armed_at=clock["now"] - 5.0,
        )
        cfg = _dump_cfg(
            shares=100.0,
            sell_dump_persist_s=0.0,
            sell_cooldown_s=0.0,
            sell_dump_fak_retries=2,
        )
        state = {"intents": {"cid-dump": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertFalse(intent.get("sold_dump"))
        self.assertAlmostEqual(intent.get("sell_dump_filled"), 26.47)
        self.assertEqual([row["size"] for row in fak_calls], [100.0, 73.53])
        clock["now"] += 1.0
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertTrue(intent.get("sold_dump"))
        self.assertAlmostEqual(fak_calls[-1]["size"], 73.53)
        done = [row for row in events if row["event"] == "sell_dump_done"]
        self.assertEqual(len(done), 1)
        self.assertAlmostEqual(done[0]["sold"], 100.0)
        self.assertAlmostEqual(done[0]["avg_px"], 0.1924)
        self.assertAlmostEqual(intent["sell_dump_fill_px"], 0.1924)
        self.assertAlmostEqual(intent["sell_dump_fill_px_shares"], 100.0)

    def test_kept_log_uses_the_total_and_the_real_fill_average(self):
        events: list = []
        ns = _sell_runtime([])
        ns["log_event"] = lambda event, **kwargs: events.append({"event": event, **kwargs})
        intent: dict = {}
        ns["_apply_kept_dump_result"](
            intent,
            "cid-kept",
            {
                "posted": True,
                "leg": "dn",
                "planned": 100.0,
                "keep": 100.0,
                "sold": 100.0,
                "swept": 0.0,
                "fills": [(26.47, 0.31), (73.53, 0.15)],
                "bid": 0.15,
                "limit": 0.15,
                "attempts": 2,
                "status": "matched",
                "outcome": "filled",
                "remaining": 0.0,
            },
        )
        kept = [row for row in events if row["event"] == "sell_dump_kept"]
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0]["sold"], 100.0)
        self.assertAlmostEqual(kept[0]["avg_px"], 0.1924)
        self.assertEqual(
            record_fill_px({}, "px", [(26.47, 0.31), (73.53, 0.15)]),
            0.1924,
        )

    def test_loser_done_sold_is_the_sum_of_sweep_chunks(self):
        calls = {"n": 0}

        def fak(_token, size, _price, _dry):
            calls["n"] += 1
            if calls["n"] == 1:
                return 20.0, "matched"
            return float(size), "matched"

        ns, events, fak_calls, clock, _book = _scrap_harness(fak)
        end = clock["now"] + 200.0
        intent = _open_scrap_bag(
            end, shares=50.0, sell_loser_armed_at=clock["now"] - 10.0,
        )
        cfg = _scrap_cfg(
            shares=50.0,
            sell_persist_s=0.0,
            sell_cooldown_s=0.0,
            sell_scrap_max_ttm_s=0,
        )
        state = {"intents": {"cid-loser": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertFalse(intent.get("sold_loser"))
        self.assertAlmostEqual(intent.get("sell_filled"), 20.0)
        self.assertEqual(len(fak_calls), 1)
        clock["now"] += 1.0
        ns["_manage_sells_locked"](cfg, state, object())
        done = [row for row in events if row["event"] == "sell_loser_done"]
        self.assertEqual(len(done), 1)
        self.assertAlmostEqual(done[0]["sold"], 50.0)
        self.assertAlmostEqual(intent.get("sell_filled"), 50.0)
        self.assertNotAlmostEqual(done[0]["sold"], fak_calls[-1]["size"])


if __name__ == "__main__":
    unittest.main()
