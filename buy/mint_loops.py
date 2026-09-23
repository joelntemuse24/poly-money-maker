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


def _market_condition_id(market: Any) -> str:
    condition_id = getattr(market, "condition_id", None)
    if condition_id is None and isinstance(market, dict):
        condition_id = market.get("condition_id")
    return str(condition_id or "")


def _market_start_ts(market: Any) -> float:
    start_ts = getattr(market, "start_ts", None)
    if start_ts is None and isinstance(market, dict):
        start_ts = market.get("start_ts")
    try:
        return float(start_ts or 0)
    except (TypeError, ValueError):
        return 0.0


def held_forward_floor(
    state: dict,
    now: float,
    active_statuses: Any,
) -> Optional[float]:
    """One 15m step after the latest active bag, or None when nothing is held.

    A confirmed (or other active) bag at ``start_ts=T`` forbids every
    candidate with ``start_ts < T+900``. Failed intents do not set the floor.
    """
    latest: Optional[float] = None
    try:
        now_ts = float(now)
    except (TypeError, ValueError):
        now_ts = 0.0
    for intent in (state.get("intents") or {}).values():
        if not isinstance(intent, dict):
            continue
        if intent.get("status") not in active_statuses:
            continue
        try:
            end_ts = float(intent.get("end_ts") or 0)
        except (TypeError, ValueError):
            end_ts = 0.0
        if end_ts and now_ts > end_ts + 120.0:
            continue
        try:
            start_ts = float(intent.get("start_ts") or 0)
        except (TypeError, ValueError):
            start_ts = 0.0
        if start_ts <= 0:
            continue
        if latest is None or start_ts > latest:
            latest = start_ts
    if latest is None:
        return None
    return latest + 900.0


def select_mint_candidate(
    candidates: list,
    is_blocked: Callable[[str], bool],
    is_owned: Optional[Callable[[Any], bool]] = None,
    slots_full: Optional[Callable[[Any], bool]] = None,
    *,
    min_start_ts: Optional[float] = None,
    fail_attempts: Optional[Callable[[str], int]] = None,
) -> tuple[Any, str]:
    """Soonest forward candidate. Virgin windows beat a failed retry.

    ``is_blocked`` covers a confirmed or in-flight mint, fail cooldown,
    and ``mint_attempts >= mint_max_attempts``. Those are skipped in this
    pass. Attempts at the cap stay blocked for that condition.

    ``min_start_ts`` is the forward floor (latest held bag start + 900s).
    Candidates that start earlier are never picked.

    When a free candidate has zero failures, a condition that already
    failed is left for a later pass. A retry is used only when no virgin
    candidate fits. ``slots_full`` still skips a candidate that does not
    fit ``max_open_sets`` without hiding a later one that does.

    Status ``capped`` means every otherwise-free candidate was over
    capacity. Status ``idle`` means nothing was free.
    """
    floor: Optional[float] = None
    if min_start_ts is not None:
        try:
            floor = float(min_start_ts)
        except (TypeError, ValueError):
            floor = None

    def fail_count(condition_id: str) -> int:
        if fail_attempts is None:
            return 0
        try:
            return int(fail_attempts(condition_id) or 0)
        except (TypeError, ValueError):
            return 0

    capped: Any = None

    def walk(allow_retry: bool) -> Any:
        nonlocal capped
        for market in candidates:
            condition_id = _market_condition_id(market)
            if not condition_id or is_blocked(condition_id):
                continue
            if is_owned is not None and is_owned(market):
                continue
            if floor is not None and _market_start_ts(market) + 1e-9 < floor:
                continue
            if not allow_retry and fail_count(condition_id) > 0:
                continue
            if slots_full is not None and slots_full(market):
                if capped is None:
                    capped = market
                continue
            return market
        return None

    picked = walk(False)
    if picked is None:
        picked = walk(True)
    if picked is not None:
        return picked, "pick"
    if capped is not None:
        return capped, "capped"
    return None, "idle"


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


# Submitted or in-flight mints whose pUSD has not been released.
# ``confirmed`` (inventory seen), ``completed``, and ``failed`` drop out,
# which is how confirm, hard fail, and the stale-submit timeout release.
PENDING_CASH_STATUSES = frozenset(
    {
        "submitting",
        "pending",
        "executed",
        "mined",
        "confirmed_waiting_inventory",
    }
)


def pending_mint_reserve(state: Any) -> float:
    """pUSD promised to mints that are submitted or in flight.

    Live ``pUSD_balance`` still shows this cash until the split lands.
    Cost is the intent ``shares`` (one pUSD per complete set).
    """
    if not isinstance(state, dict):
        return 0.0
    intents = state.get("intents") or {}
    if not isinstance(intents, dict):
        return 0.0
    total = 0.0
    for intent in intents.values():
        if not isinstance(intent, dict):
            continue
        if str(intent.get("status") or "") not in PENDING_CASH_STATUSES:
            continue
        try:
            shares = float(intent.get("shares") or 0)
        except (TypeError, ValueError):
            continue
        if shares > 0:
            total += shares
    return total


def mint_cash_block(balance: float, need: float, reserved: float) -> Optional[dict]:
    """None when ``balance - reserved`` covers ``need``.

    ``pending_reserve`` means an in-flight mint is still holding cash the
    live balance shows as free. ``no_balance`` is a short wallet with
    nothing reserved.
    """
    try:
        bal = float(balance)
        cost = float(need)
        held = float(reserved)
    except (TypeError, ValueError):
        return {
            "reason": "no_balance",
            "balance": balance,
            "reserved": reserved,
            "free": None,
            "need": need,
        }
    free = bal - held
    if free + 1e-9 >= cost:
        return None
    reason = "pending_reserve" if held > 1e-9 else "no_balance"
    return {
        "reason": reason,
        "balance": bal,
        "reserved": held,
        "free": free,
        "need": cost,
    }
