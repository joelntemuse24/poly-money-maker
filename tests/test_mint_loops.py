"""Concurrent mint/sell loops (no mintbot import, no CLOB)."""

from __future__ import annotations

import threading
import time
import unittest

from types import SimpleNamespace

from buy.mint_loops import (
    IntentStore,
    interruptible_sleep,
    select_mint_candidate,
    start_mint_sell_loops,
)
from buy.mint_sell import persist_ready


_BLOCK_STATUSES = frozenset(
    {
        "submitting",
        "pending",
        "executed",
        "mined",
        "confirmed_waiting_inventory",
        "confirmed",
        "completed",
    }
)


def _blocked(current: dict | None) -> bool:
    if not current:
        return False
    return current.get("status") in _BLOCK_STATUSES


class IntentStoreClaimTests(unittest.TestCase):
    def test_second_claim_on_same_slug_loses(self):
        store = IntentStore()
        first = store.try_claim_condition(
            "cid-a",
            {"status": "submitting", "slug": "btc-updown-15m-1"},
            is_blocked=_blocked,
        )
        second = store.try_claim_condition(
            "cid-a",
            {"status": "submitting", "slug": "btc-updown-15m-1-dup"},
            is_blocked=_blocked,
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(store.state["intents"]["cid-a"]["slug"], "btc-updown-15m-1")

    def test_failed_intent_is_not_blocked_by_default_predicate(self):
        store = IntentStore(
            {"intents": {"cid-a": {"status": "failed", "mint_attempts": 1}}}
        )
        claimed = store.try_claim_condition(
            "cid-a",
            {"status": "submitting", "mint_attempts": 2},
            is_blocked=_blocked,
        )
        self.assertTrue(claimed)
        self.assertEqual(store.state["intents"]["cid-a"]["status"], "submitting")

    def test_concurrent_claims_only_one_wins(self):
        store = IntentStore()
        barrier = threading.Barrier(8)
        wins = []
        lock = threading.Lock()

        def claim(idx: int) -> None:
            barrier.wait(timeout=2)
            ok = store.try_claim_condition(
                "same-slug",
                {"status": "submitting", "who": idx},
                is_blocked=_blocked,
            )
            if ok:
                with lock:
                    wins.append(idx)

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(len(wins), 1)
        self.assertEqual(store.state["intents"]["same-slug"]["status"], "submitting")


class ConcurrentLoopTests(unittest.TestCase):
    def test_sell_ticks_while_mint_in_progress(self):
        mint_started = threading.Event()
        sell_during_mint = threading.Event()
        stop = threading.Event()
        sell_times: list[float] = []

        def mint_tick() -> None:
            mint_started.set()
            time.sleep(0.35)
            stop.set()

        def sell_tick() -> None:
            if mint_started.is_set() and not stop.is_set():
                sell_times.append(time.monotonic())
                sell_during_mint.set()

        sell_t, mint_t = start_mint_sell_loops(
            sell_tick=sell_tick,
            sell_sleep_s=lambda: 0.02,
            mint_tick=mint_tick,
            mint_sleep_s=lambda: 0.02,
            should_stop=stop.is_set,
        )
        self.assertTrue(sell_during_mint.wait(2.0), "sell never ticked during mint I/O")
        self.assertGreaterEqual(len(sell_times), 1)
        stop.set()
        sell_t.join(timeout=2)
        mint_t.join(timeout=2)

    def test_mint_proceeds_while_sell_manages_other_intent(self):
        sell_started = threading.Event()
        mint_during_sell = threading.Event()
        stop = threading.Event()
        mint_times: list[float] = []

        def sell_tick() -> None:
            sell_started.set()
            time.sleep(0.35)
            stop.set()

        def mint_tick() -> None:
            if sell_started.is_set() and not stop.is_set():
                mint_times.append(time.monotonic())
                mint_during_sell.set()

        sell_t, mint_t = start_mint_sell_loops(
            sell_tick=sell_tick,
            sell_sleep_s=lambda: 0.02,
            mint_tick=mint_tick,
            mint_sleep_s=lambda: 0.02,
            should_stop=stop.is_set,
        )
        self.assertTrue(
            mint_during_sell.wait(2.0),
            "mint never ticked while sell managed another intent",
        )
        self.assertGreaterEqual(len(mint_times), 1)
        stop.set()
        sell_t.join(timeout=2)
        mint_t.join(timeout=2)

    def test_hot_path_still_respects_persist(self):
        persist_s = 0.25
        armed_ts = time.time()
        stop = threading.Event()
        fires: list[tuple[float, bool, str]] = []

        def sell_tick() -> None:
            now = time.time()
            fire, _armed, why = persist_ready(
                True,
                now_s=now,
                armed_ts=armed_ts,
                persist_s=persist_s,
            )
            fires.append((now - armed_ts, fire, why))
            if fire:
                stop.set()

        def mint_tick() -> None:
            time.sleep(0.20)

        sell_t, mint_t = start_mint_sell_loops(
            sell_tick=sell_tick,
            sell_sleep_s=lambda: 0.03,
            mint_tick=mint_tick,
            mint_sleep_s=lambda: 0.03,
            should_stop=stop.is_set,
        )
        sell_t.join(timeout=3)
        mint_t.join(timeout=3)
        self.assertTrue(any(fire for _dt, fire, _why in fires), fires)
        first_fire_dt = next(dt for dt, fire, _why in fires if fire)
        self.assertGreaterEqual(first_fire_dt, persist_s - 0.03)
        waiting = [row for row in fires if row[0] < persist_s - 0.03]
        self.assertTrue(waiting)
        self.assertFalse(any(fire for _dt, fire, _why in waiting))

    def test_sibling_error_does_not_stop_other_loop(self):
        stop = threading.Event()
        sell_ok = threading.Event()
        mint_errors: list[str] = []

        def mint_tick() -> None:
            raise RuntimeError("mint_boom")

        def sell_tick() -> None:
            if mint_errors:
                sell_ok.set()
                stop.set()

        sell_t, mint_t = start_mint_sell_loops(
            sell_tick=sell_tick,
            sell_sleep_s=lambda: 0.01,
            mint_tick=mint_tick,
            mint_sleep_s=lambda: 0.01,
            should_stop=stop.is_set,
            on_mint_error=lambda exc: mint_errors.append(str(exc)),
        )
        self.assertTrue(sell_ok.wait(2.0), "sell never ticked after mint error")
        stop.set()
        sell_t.join(timeout=2)
        mint_t.join(timeout=2)
        self.assertTrue(mint_errors)


def _market(condition_id: str, start_ts: float):
    return SimpleNamespace(
        condition_id=condition_id,
        start_ts=start_ts,
        slug=f"btc-updown-15m-{int(start_ts)}",
    )


class SelectMintCandidateTests(unittest.TestCase):
    def test_cooldown_on_nearest_picks_next_future_same_cycle(self):
        nearest = _market("near", 100.0)
        later = _market("later", 200.0)
        pick, status = select_mint_candidate(
            [nearest, later],
            is_blocked=lambda cid: cid == "near",
        )
        self.assertEqual(status, "pick")
        self.assertIs(pick, later)

    def test_exhausted_condition_is_not_reminted(self):
        poisoned = _market("poison", 100.0)
        later = _market("later", 200.0)
        pick, status = select_mint_candidate(
            [poisoned, later],
            is_blocked=lambda cid: cid == "poison",
        )
        self.assertEqual(status, "pick")
        self.assertEqual(pick.condition_id, "later")

    def test_only_blocked_candidates_idle(self):
        pick, status = select_mint_candidate(
            [_market("near", 100.0)],
            is_blocked=lambda cid: True,
        )
        self.assertIsNone(pick)
        self.assertEqual(status, "idle")

    def test_after_cooldown_retries_nearest_when_no_virgin_remains(self):
        nearest = _market("near", 100.0)
        later = _market("later", 200.0)
        pick, status = select_mint_candidate(
            [nearest, later],
            is_blocked=lambda cid: False,
            fail_attempts=lambda cid: 1,
        )
        self.assertEqual(status, "pick")
        self.assertIs(pick, nearest)

    def test_virgin_beats_failed_retry_while_a_slot_is_free(self):
        failed = _market("failed", 100.0)
        virgin = _market("virgin", 1_000.0)
        pick, status = select_mint_candidate(
            [failed, virgin],
            is_blocked=lambda cid: False,
            fail_attempts=lambda cid: 2 if cid == "failed" else 0,
        )
        self.assertEqual(status, "pick")
        self.assertIs(pick, virgin)

    def test_held_bag_floor_skips_every_earlier_start(self):
        earlier = _market("earlier", 1_000.0)
        held = _market("held", 1_900.0)
        nxt = _market("next", 2_800.0)
        pick, status = select_mint_candidate(
            [earlier, held, nxt],
            is_blocked=lambda cid: cid == "held",
            min_start_ts=1_900.0 + 900.0,
        )
        self.assertEqual(status, "pick")
        self.assertIs(pick, nxt)

    def test_capacity_blocked_nearest_does_not_hide_adjacent_window(self):
        nearer = _market("near", 100.0)
        adjacent = _market("next", 1_000.0)
        too_far = _market("far", 2_000.0)
        pick, status = select_mint_candidate(
            [nearer, adjacent, too_far],
            is_blocked=lambda cid: False,
            slots_full=lambda market: market.condition_id != "next",
        )
        self.assertEqual(status, "pick")
        self.assertIs(pick, adjacent)

    def test_all_over_cap_reports_capped(self):
        first = _market("a", 100.0)
        second = _market("b", 200.0)
        pick, status = select_mint_candidate(
            [first, second],
            is_blocked=lambda cid: False,
            slots_full=lambda market: True,
        )
        self.assertEqual(status, "capped")
        self.assertIs(pick, first)

    def test_owned_tokens_are_skipped(self):
        held = _market("held", 100.0)
        fresh = _market("fresh", 200.0)
        pick, status = select_mint_candidate(
            [held, fresh],
            is_blocked=lambda cid: False,
            is_owned=lambda market: market.condition_id == "held",
        )
        self.assertEqual(status, "pick")
        self.assertIs(pick, fresh)


class HeldForwardFloorTests(unittest.TestCase):
    def test_floor_is_one_window_after_the_latest_active_bag(self):
        from buy.mint_loops import held_forward_floor

        state = {
            "intents": {
                "early": {"status": "confirmed", "start_ts": 1_000.0, "end_ts": 1_900.0},
                "late": {"status": "confirmed", "start_ts": 1_900.0, "end_ts": 2_800.0},
                "dead": {"status": "failed", "start_ts": 2_800.0, "end_ts": 3_700.0},
            }
        }
        floor = held_forward_floor(state, now=1_500.0, active_statuses={"confirmed"})
        self.assertEqual(floor, 1_900.0 + 900.0)

    def test_no_floor_without_an_active_bag(self):
        from buy.mint_loops import held_forward_floor

        state = {"intents": {"x": {"status": "failed", "start_ts": 1_000.0, "end_ts": 1_900.0}}}
        self.assertIsNone(
            held_forward_floor(state, now=1_100.0, active_statuses={"confirmed"})
        )


class InterruptibleSleepTests(unittest.TestCase):
    def test_stop_cuts_sleep_short(self):
        stop = threading.Event()

        def cutter() -> None:
            time.sleep(0.05)
            stop.set()

        threading.Thread(target=cutter, daemon=True).start()
        t0 = time.monotonic()
        interruptible_sleep(2.0, stop.is_set, slice_s=0.02)
        self.assertLess(time.monotonic() - t0, 0.5)


if __name__ == "__main__":
    unittest.main()
