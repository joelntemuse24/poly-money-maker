"""Concurrent mint vs sell job loops (no CLOB / relayer I/O).

The live hole (bag ``btc-updown-15m-1789905600``) was a single thread:
``manage_sells → Gamma/mint → sleep``. After ``loser_done``, next-window
mint ran synchronously and delayed the first dump look ~16s. These
helpers run sell and mint as independent jobs. They share intent state
only through a short lock; I/O stays outside that lock.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

StopFn = Callable[[], bool]
TickFn = Callable[[], None]
SleepFn = Callable[[], float]
ErrorFn = Callable[[BaseException], None]
BlockedFn = Callable[[Optional[dict]], bool]
MutateFn = Callable[[dict], Any]


class IntentStore:
    """In-memory intent map with a short critical-section lock.

    Callers must keep CLOB / Gamma / relayer / RPC I/O outside
    ``mutate`` and ``try_claim_condition``.
    """

    def __init__(
        self,
        state: Optional[dict] = None,
        lock: Optional[threading.RLock] = None,
    ) -> None:
        self.state = state if state is not None else {"intents": {}}
        self.lock = lock if lock is not None else threading.RLock()

    def mutate(self, fn: MutateFn) -> Any:
        with self.lock:
            return fn(self.state)

    def try_claim_condition(
        self,
        condition_id: str,
        intent: dict,
        *,
        is_blocked: BlockedFn,
    ) -> bool:
        """Atomically claim ``condition_id`` unless ``is_blocked(current)``.

        Prevents two mint ticks from writing ``submitting`` for the same
        slug. The mint loop is single-threaded; this is the race guard
        if two claim attempts overlap on the shared store.
        """
        if not condition_id:
            return False
        with self.lock:
            intents = self.state.setdefault("intents", {})
            if not isinstance(intents, dict):
                intents = {}
                self.state["intents"] = intents
            current = intents.get(condition_id)
            if is_blocked(current if isinstance(current, dict) else None):
                return False
            intents[condition_id] = intent
            return True


def interruptible_sleep(
    seconds: float,
    should_stop: StopFn,
    slice_s: float = 0.05,
) -> None:
    """Sleep ``seconds`` in slices so shutdown does not wait out poll_s."""
    try:
        delay = float(seconds or 0)
    except (TypeError, ValueError):
        delay = 0.0
    if delay <= 0:
        return
    try:
        slice_s = float(slice_s)
    except (TypeError, ValueError):
        slice_s = 0.05
    if slice_s <= 0:
        slice_s = 0.05
    deadline = time.monotonic() + delay
    while not should_stop():
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(slice_s, left))


def run_job_loop(
    *,
    name: str,
    tick: TickFn,
    sleep_s: SleepFn,
    should_stop: StopFn,
    on_error: Optional[ErrorFn] = None,
) -> None:
    """Repeat ``tick`` then ``sleep_s()`` until ``should_stop``.

    ``name`` is for thread identification only. A tick exception does
    not stop the sibling job.
    """
    del name
    while not should_stop():
        try:
            tick()
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
        if should_stop():
            break
        try:
            delay = float(sleep_s() or 0)
        except Exception:
            delay = 0.0
        interruptible_sleep(delay, should_stop)


def start_mint_sell_loops(
    *,
    sell_tick: TickFn,
    sell_sleep_s: SleepFn,
    mint_tick: TickFn,
    mint_sleep_s: SleepFn,
    should_stop: StopFn,
    on_sell_error: Optional[ErrorFn] = None,
    on_mint_error: Optional[ErrorFn] = None,
) -> tuple[threading.Thread, threading.Thread]:
    """Start daemon sell and mint loops. Caller joins on shutdown."""
    sell_thread = threading.Thread(
        target=run_job_loop,
        kwargs={
            "name": "sell",
            "tick": sell_tick,
            "sleep_s": sell_sleep_s,
            "should_stop": should_stop,
            "on_error": on_sell_error,
        },
        name="mintbot-sell",
        daemon=True,
    )
    mint_thread = threading.Thread(
        target=run_job_loop,
        kwargs={
            "name": "mint",
            "tick": mint_tick,
            "sleep_s": mint_sleep_s,
            "should_stop": should_stop,
            "on_error": on_mint_error,
        },
        name="mintbot-mint",
        daemon=True,
    )
    sell_thread.start()
    mint_thread.start()
    return sell_thread, mint_thread
