"""Auto-redeem resolved positions (opt-in ``redeem_enabled``).

One job per condition lives in ``state["redeems"][condition_id]``. Jobs come
from ended, landed mint bags (``end_ts + redeem_min_after_end_s``) and, once
per process start, from Data API positions marked ``redeemable``.

Per job and tick:

* Both legs at or under tolerance on two reads ``redeem_poll_s`` apart:
  ``done`` / ``nothing_held`` (fully sold, or redeemed elsewhere). One zero
  read is not proof.
* ``payoutDenominator == 0``: not resolved yet, wait (slower after 1h).
* Resolved, but held legs pay under ``redeem_min_payout_usd``:
  ``no_winner``. No transaction is sent for a worthless kept loser.
* ``dry_run``: log ``redeem_dry_run`` once and re-check every 5 minutes.
  Nothing is sent, and the job stays open for a later live run.
* Otherwise persist ``submitting``, then one relayer PROXY batch
  (``setApprovalForAll(adapter)`` only if missing, then
  ``adapter.redeemPositions``). At most one submit per tick.
* ``submitted``: poll the relayer. Confirmed with both legs cleared is
  ``done``. Failed, invalid, or no answer past ``redeem_tx_timeout_s``
  (with the legs still held) backs off and retries up to
  ``redeem_max_attempts``, then ``gave_up``.

A repeated redeem is harmless on chain (it burns a zero balance and pays
zero), so a retry after an uncertain submit cannot double-pay or lose
funds. All chain and relayer I/O runs outside ``lock``.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

REDEEM_POLL_S = 15.0
REDEEM_MIN_AFTER_END_S = 60.0
REDEEM_RETRY_S = 60.0
REDEEM_RETRY_CAP_S = 900.0
REDEEM_MAX_ATTEMPTS = 6
REDEEM_TX_TIMEOUT_S = 300.0
REDEEM_MIN_PAYOUT_USD = 0.01
REDEEM_SLOW_AFTER_S = 3600.0
REDEEM_SLOW_POLL_S = 300.0
REDEEM_SWEEP_RETRY_S = 300.0
REDEEM_JOBS_PER_TICK = 8
REDEEM_PRUNE_AFTER_S = 2 * 86400.0

LANDED_STATUSES = frozenset({"mined", "confirmed_waiting_inventory", "confirmed"})
FINAL_STATUSES = frozenset({"done", "no_winner", "gave_up"})
CONFIRMED_STATES = frozenset({"STATE_CONFIRMED"})
FAILED_STATES = frozenset({"STATE_FAILED", "STATE_INVALID"})


def _num(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


@dataclass(frozen=True)
class RedeemSettings:
    enabled: bool = False
    dry_run: bool = True
    poll_s: float = REDEEM_POLL_S
    min_after_end_s: float = REDEEM_MIN_AFTER_END_S
    retry_s: float = REDEEM_RETRY_S
    max_attempts: int = REDEEM_MAX_ATTEMPTS
    tx_timeout_s: float = REDEEM_TX_TIMEOUT_S
    startup_sweep: bool = True
    min_payout_usd: float = REDEEM_MIN_PAYOUT_USD
    tolerance: float = 0.01


def redeem_settings(cfg: Any) -> RedeemSettings:
    cfg = cfg or {}
    try:
        attempts = int(cfg.get("redeem_max_attempts", REDEEM_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        attempts = REDEEM_MAX_ATTEMPTS
    return RedeemSettings(
        enabled=bool(cfg.get("redeem_enabled", False)),
        dry_run=bool(cfg.get("dry_run", True)),
        poll_s=max(1.0, _num(cfg.get("redeem_poll_s", REDEEM_POLL_S), REDEEM_POLL_S)),
        min_after_end_s=max(
            0.0,
            _num(cfg.get("redeem_min_after_end_s", REDEEM_MIN_AFTER_END_S), REDEEM_MIN_AFTER_END_S),
        ),
        retry_s=max(1.0, _num(cfg.get("redeem_retry_s", REDEEM_RETRY_S), REDEEM_RETRY_S)),
        max_attempts=max(1, attempts),
        tx_timeout_s=max(
            30.0, _num(cfg.get("redeem_tx_timeout_s", REDEEM_TX_TIMEOUT_S), REDEEM_TX_TIMEOUT_S)
        ),
        startup_sweep=bool(cfg.get("redeem_startup_sweep", True)),
        min_payout_usd=max(
            0.0,
            _num(cfg.get("redeem_min_payout_usd", REDEEM_MIN_PAYOUT_USD), REDEEM_MIN_PAYOUT_USD),
        ),
        tolerance=max(0.0, _num(cfg.get("position_tolerance", 0.01), 0.01)),
    )


def validate_redeem(cfg: Any) -> None:
    cfg = cfg or {}
    for key in (
        "redeem_poll_s",
        "redeem_retry_s",
        "redeem_tx_timeout_s",
    ):
        raw = cfg.get(key)
        if raw is not None and _num(raw, -1.0) <= 0:
            raise ValueError(f"{key} must be > 0")
    for key in ("redeem_min_after_end_s", "redeem_min_payout_usd"):
        raw = cfg.get(key)
        if raw is not None and _num(raw, -1.0) < 0:
            raise ValueError(f"{key} must be >= 0")
    raw = cfg.get("redeem_max_attempts")
    if raw is not None:
        try:
            if int(raw) < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("redeem_max_attempts must be >= 1")


def retry_delay_s(settings: RedeemSettings, attempts: int) -> float:
    """``retry_s`` doubling per failed attempt, capped at 15 minutes."""
    step = max(0, int(attempts) - 1)
    return min(REDEEM_RETRY_CAP_S, float(settings.retry_s) * (2 ** min(step, 10)))


def sweep_jobs_from_positions(rows: Iterable[Any]) -> tuple[list[dict], int]:
    """Redeemable Data API rows grouped per condition.

    Returns ``(jobs, neg_risk_conditions)``. Up is outcome index 0, the
    same order as the market's ``clobTokenIds``.
    """
    by_cid: dict[str, dict] = {}
    neg_risk: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("redeemable"):
            continue
        cid = str(row.get("conditionId") or "").lower()
        asset = str(row.get("asset") or "")
        if not cid.startswith("0x") or len(cid) != 66 or not asset:
            continue
        if _num(row.get("size"), 0.0) <= 0:
            continue
        if row.get("negativeRisk"):
            neg_risk.add(cid)
            continue
        try:
            index = int(row.get("outcomeIndex"))
        except (TypeError, ValueError):
            continue
        opposite = str(row.get("oppositeAsset") or "")
        if index not in (0, 1) or not opposite:
            continue
        up, dn = (asset, opposite) if index == 0 else (opposite, asset)
        by_cid.setdefault(
            cid,
            {"condition_id": cid, "slug": str(row.get("slug") or ""), "up_token": up, "dn_token": dn},
        )
    return list(by_cid.values()), len(neg_risk)


@dataclass
class RedeemIO:
    """Chain, relayer, and notification hooks. Every call may raise."""

    payout_denominator: Callable[[str], int]
    payout_numerator: Callable[[str, int], int]
    balance: Callable[[str], float]
    is_approved: Callable[[], bool]
    submit: Callable[[str, bool], tuple]
    relayer_status: Callable[[str], Optional[dict]]
    log: Callable[..., None]
    notify: Callable[[str, str], None] = lambda title, message: None
    positions: Optional[Callable[[], list]] = None


class RedeemDesk:
    """Runs redeem jobs against shared bot ``state``.

    ``lock`` guards ``state``. ``save(state)`` persists it and is called
    with ``lock`` held.
    """

    def __init__(self, io: RedeemIO, *, lock: Any = None, save: Callable[[dict], Any] = lambda s: None):
        self.io = io
        self.lock = lock if lock is not None else threading.RLock()
        self.save = save
        self.sweep_done = False
        self.sweep_next_at = 0.0

    # -- state helpers -------------------------------------------------

    @staticmethod
    def jobs(state: dict) -> dict:
        jobs = state.get("redeems")
        if not isinstance(jobs, dict):
            jobs = {}
            state["redeems"] = jobs
        return jobs

    def _write(self, state: dict, cid: str, job: dict) -> None:
        with self.lock:
            self.jobs(state)[cid] = job
            self._sync_intent(state, cid, job)
            self.save(state)

    @staticmethod
    def _sync_intent(state: dict, cid: str, job: dict) -> None:
        intent = (state.get("intents") or {}).get(cid)
        if not isinstance(intent, dict):
            return
        status = str(job.get("status") or "")
        intent["redeem_status"] = status
        if status == "done" and job.get("reason") == "redeemed":
            intent["redeemed"] = True
        if status == "gave_up":
            intent["redeem_gave_up"] = True
        if status in ("done", "no_winner") and intent.get("status") in LANDED_STATUSES:
            intent["status"] = "completed"
            intent["updated_at"] = job.get("updated_at") or intent.get("updated_at")

    # -- job creation ---------------------------------------------------

    def enqueue_ended_bags(self, state: dict, settings: RedeemSettings, now: float) -> int:
        """Create jobs for landed non-dry bags past ``end + min_after_end``. Caller holds lock."""
        jobs = self.jobs(state)
        added = 0
        for cid, intent in list((state.get("intents") or {}).items()):
            if not isinstance(intent, dict) or cid in jobs:
                continue
            if intent.get("dry_run") or intent.get("redeem_gave_up") or intent.get("redeemed"):
                continue
            if str(intent.get("status") or "") not in LANDED_STATUSES:
                continue
            end = _num(intent.get("end_ts"), 0.0)
            if not end or now < end + settings.min_after_end_s:
                continue
            up = str(intent.get("up_token") or "")
            dn = str(intent.get("dn_token") or "")
            if not up or not dn:
                continue
            jobs[str(cid)] = new_job(
                str(cid), up, dn, now, slug=str(intent.get("slug") or ""), end_ts=end, source="bag"
            )
            added += 1
        return added

    def startup_sweep(self, state: dict, settings: RedeemSettings, now: float) -> Optional[int]:
        """One Data API pass for leftover redeemable positions. None until it succeeds."""
        if self.sweep_done or not settings.startup_sweep or self.io.positions is None:
            self.sweep_done = True
            return None
        if now < self.sweep_next_at:
            return None
        try:
            rows = self.io.positions()
        except Exception as exc:
            self.sweep_next_at = now + REDEEM_SWEEP_RETRY_S
            self.io.log("redeem_sweep_fail", error=str(exc)[:160])
            return None
        found, neg_risk = sweep_jobs_from_positions(rows)
        added = 0
        with self.lock:
            jobs = self.jobs(state)
            for row in found:
                cid = row["condition_id"]
                if cid in jobs:
                    continue
                intent = (state.get("intents") or {}).get(cid)
                if isinstance(intent, dict) and (intent.get("redeemed") or intent.get("redeem_gave_up")):
                    continue
                jobs[cid] = new_job(
                    cid, row["up_token"], row["dn_token"], now, slug=row["slug"], source="startup"
                )
                added += 1
            if added:
                self.save(state)
        self.sweep_done = True
        self.io.log("redeem_sweep", found=len(found), added=added, neg_risk_skipped=neg_risk)
        return added

    # -- tick ------------------------------------------------------------

    def tick(self, state: dict, cfg: Any, now: float) -> dict:
        """One pass. Returns counters for the heartbeat."""
        settings = redeem_settings(cfg)
        if not settings.enabled:
            return {"status": "disabled"}
        self.startup_sweep(state, settings, now)
        with self.lock:
            if self.enqueue_ended_bags(state, settings, now):
                self.save(state)
            pruned = self._prune(state, now)
            if pruned:
                self.save(state)
            due = [
                (cid, dict(job))
                for cid, job in self.jobs(state).items()
                if isinstance(job, dict)
                and job.get("status") not in FINAL_STATUSES
                and now >= _num(job.get("next_at"), 0.0)
            ]
        due.sort(key=lambda item: _num(item[1].get("created_at"), 0.0))
        submitted = 0
        for cid, job in due[:REDEEM_JOBS_PER_TICK]:
            try:
                did_submit = self.step(state, cid, job, settings, now, may_submit=submitted == 0)
            except Exception as exc:
                self.io.log("redeem_check_fail", condition_id=cid, error=str(exc)[:160])
                job["next_at"] = now + settings.poll_s
                self._write(state, cid, job)
                continue
            submitted += int(bool(did_submit))
        with self.lock:
            open_jobs = sum(
                1
                for job in self.jobs(state).values()
                if isinstance(job, dict) and job.get("status") not in FINAL_STATUSES
            )
        return {"status": "ok", "due": len(due), "submitted": submitted, "open": open_jobs}

    def _prune(self, state: dict, now: float) -> int:
        jobs = self.jobs(state)
        stale = [
            cid
            for cid, job in jobs.items()
            if not isinstance(job, dict)
            or (
                job.get("status") in FINAL_STATUSES
                and now - _num(job.get("updated_at"), now) > REDEEM_PRUNE_AFTER_S
            )
        ]
        for cid in stale:
            jobs.pop(cid, None)
        return len(stale)

    def _legs(self, job: dict) -> tuple[float, float]:
        return float(self.io.balance(job["up_token"])), float(self.io.balance(job["dn_token"]))

    def _finish(self, state: dict, cid: str, job: dict, status: str, now: float, **fields: Any) -> None:
        job["status"] = status
        job["updated_at"] = now
        job["done_at"] = now
        job.update(fields)
        self._write(state, cid, job)

    def step(
        self,
        state: dict,
        cid: str,
        job: dict,
        settings: RedeemSettings,
        now: float,
        *,
        may_submit: bool = True,
    ) -> bool:
        """Advance one job. Returns True when it posted a relayer submit."""
        status = str(job.get("status") or "waiting")
        if status == "submitted":
            self._poll_submitted(state, cid, job, settings, now)
            return False
        if status == "submitting":
            return self._recover_submitting(state, cid, job, settings, now)
        return self._check_and_submit(state, cid, job, settings, now, may_submit=may_submit)

    def _check_and_submit(
        self,
        state: dict,
        cid: str,
        job: dict,
        settings: RedeemSettings,
        now: float,
        *,
        may_submit: bool,
    ) -> bool:
        tol = settings.tolerance
        up, dn = self._legs(job)
        job["observed_up"], job["observed_dn"] = up, dn
        if max(up, dn) <= tol:
            job["zero_reads"] = int(job.get("zero_reads") or 0) + 1
            if job["zero_reads"] >= 2:
                self._finish(state, cid, job, "done", now, reason="nothing_held")
                self.io.log(
                    "redeem_nothing_held",
                    condition_id=cid,
                    slug=job.get("slug"),
                    source=job.get("source"),
                    attempts=job.get("attempts", 0),
                )
                return False
            job["next_at"] = now + settings.poll_s
            self._write(state, cid, job)
            return False
        job["zero_reads"] = 0

        den = int(self.io.payout_denominator(cid))
        if den <= 0:
            if not job.get("wait_logged"):
                job["wait_logged"] = True
                self.io.log(
                    "redeem_wait_resolution",
                    condition_id=cid,
                    slug=job.get("slug"),
                    up=up,
                    dn=dn,
                    since_end_s=round(now - _num(job.get("end_ts"), now), 1),
                )
            age = now - _num(job.get("created_at"), now)
            wait = REDEEM_SLOW_POLL_S if age > REDEEM_SLOW_AFTER_S else settings.poll_s
            job["next_at"] = now + wait
            self._write(state, cid, job)
            return False

        num_up = int(self.io.payout_numerator(cid, 0))
        num_dn = int(self.io.payout_numerator(cid, 1))
        payout = (up * num_up + dn * num_dn) / float(den)
        job["payout_est"] = round(payout, 6)
        job["payout_numerators"] = [num_up, num_dn]
        if payout < settings.min_payout_usd:
            self._finish(state, cid, job, "no_winner", now, reason="worthless")
            self.io.log(
                "redeem_no_winner",
                condition_id=cid,
                slug=job.get("slug"),
                up=up,
                dn=dn,
                numerators=[num_up, num_dn],
            )
            return False

        if settings.dry_run:
            if not job.get("dry_logged"):
                job["dry_logged"] = True
                self.io.log(
                    "redeem_dry_run",
                    condition_id=cid,
                    slug=job.get("slug"),
                    payout_est=job["payout_est"],
                )
            job["next_at"] = now + REDEEM_SLOW_POLL_S
            self._write(state, cid, job)
            return False

        if not may_submit:
            return False
        if int(job.get("attempts") or 0) >= settings.max_attempts:
            self._give_up(state, cid, job, now, "max_attempts")
            return False

        approve = not bool(self.io.is_approved())
        job["attempts"] = int(job.get("attempts") or 0) + 1
        job["status"] = "submitting"
        job["submitting_at"] = now
        job["updated_at"] = now
        job["tx_id"] = None
        self._write(state, cid, job)
        try:
            result = self.io.submit(cid, approve)
        except Exception as exc:
            result = (None, f"submit raised: {str(exc)[:160]}", {})
        tx_id, err, gas = (tuple(result) + (None, None, {}))[:3]
        gas = gas if isinstance(gas, dict) else {}
        if not tx_id:
            self._fail(state, cid, job, settings, now, str(err or "no transaction id"), gas=gas)
            return True
        job["status"] = "submitted"
        job["tx_id"] = str(tx_id)
        job["submitted_at"] = now
        job["next_at"] = now + settings.poll_s
        self._write(state, cid, job)
        self.io.log(
            "redeem_submitted",
            condition_id=cid,
            slug=job.get("slug"),
            transaction_id=str(tx_id),
            attempts=job["attempts"],
            approve_adapter=approve,
            payout_est=job.get("payout_est"),
            up=up,
            dn=dn,
            gas_limit=gas.get("gas_limit"),
            gas_estimate=gas.get("gas_estimate"),
            gas_source=gas.get("gas_source"),
        )
        return True

    def _poll_submitted(
        self, state: dict, cid: str, job: dict, settings: RedeemSettings, now: float
    ) -> None:
        tx_id = str(job.get("tx_id") or "")
        record = self.io.relayer_status(tx_id) if tx_id else None
        relayer_state = str((record or {}).get("state") or "")
        if relayer_state:
            job["relayer_state"] = relayer_state
        if relayer_state in FAILED_STATES:
            msg = str((record or {}).get("errorMsg") or (record or {}).get("error") or relayer_state)
            tx_hash = str((record or {}).get("transactionHash") or "")
            if tx_hash:
                job["tx_hash"] = tx_hash
            self._fail(state, cid, job, settings, now, msg[:400])
            return
        timed_out = now - _num(job.get("submitted_at"), now) > settings.tx_timeout_s
        if relayer_state not in CONFIRMED_STATES and not timed_out:
            job["next_at"] = now + settings.poll_s
            self._write(state, cid, job)
            return
        if record and record.get("transactionHash"):
            job["tx_hash"] = str(record.get("transactionHash"))
        up, dn = self._legs(job)
        job["observed_up"], job["observed_dn"] = up, dn
        if max(up, dn) <= settings.tolerance:
            self._finish(state, cid, job, "done", now, reason="redeemed")
            self.io.log(
                "redeem_confirmed",
                condition_id=cid,
                slug=job.get("slug"),
                transaction_id=tx_id,
                tx_hash=job.get("tx_hash"),
                relayer_state=relayer_state,
                payout_est=job.get("payout_est"),
                attempts=job.get("attempts"),
                source=job.get("source"),
            )
            self.io.notify(
                "Redeemed",
                f"{job.get('slug') or cid[:12]}\n~${_num(job.get('payout_est'), 0.0):.2f} pUSD",
            )
            return
        if not timed_out:
            job["next_at"] = now + settings.poll_s
            self._write(state, cid, job)
            return
        reason = "confirmed_not_cleared" if relayer_state in CONFIRMED_STATES else "tx_timeout"
        self._fail(state, cid, job, settings, now, reason)

    def _recover_submitting(
        self, state: dict, cid: str, job: dict, settings: RedeemSettings, now: float
    ) -> bool:
        """A crash between persisting ``submitting`` and recording the tx id."""
        if now - _num(job.get("submitting_at"), now) <= settings.tx_timeout_s:
            job["next_at"] = now + settings.poll_s
            self._write(state, cid, job)
            return False
        self.io.log("redeem_submit_uncertain", condition_id=cid, attempts=job.get("attempts"))
        job["status"] = "waiting"
        job["next_at"] = now
        job["zero_reads"] = 0
        self._write(state, cid, job)
        return False

    def _fail(
        self,
        state: dict,
        cid: str,
        job: dict,
        settings: RedeemSettings,
        now: float,
        error: str,
        *,
        gas: Optional[dict] = None,
    ) -> None:
        attempts = int(job.get("attempts") or 0)
        job["last_error"] = str(error)[:400]
        job["updated_at"] = now
        gas = gas or {}
        self.io.log(
            "redeem_submit_fail",
            condition_id=cid,
            slug=job.get("slug"),
            error=job["last_error"],
            attempts=attempts,
            transaction_id=job.get("tx_id"),
            tx_hash=job.get("tx_hash"),
            gas_limit=gas.get("gas_limit"),
            gas_estimate=gas.get("gas_estimate"),
        )
        if attempts >= settings.max_attempts:
            self._give_up(state, cid, job, now, job["last_error"])
            return
        job["status"] = "waiting"
        job["tx_id"] = None
        job["zero_reads"] = 0
        job["next_at"] = now + retry_delay_s(settings, attempts)
        self._write(state, cid, job)

    def _give_up(self, state: dict, cid: str, job: dict, now: float, error: str) -> None:
        self._finish(state, cid, job, "gave_up", now, reason=str(error)[:200])
        self.io.log(
            "redeem_gave_up",
            condition_id=cid,
            slug=job.get("slug"),
            attempts=job.get("attempts"),
            error=str(error)[:200],
        )
        self.io.notify(
            "Redeem gave up",
            f"{job.get('slug') or cid[:12]}\n{str(error)[:120]}\nredeem manually",
        )


def new_job(
    cid: str,
    up_token: str,
    dn_token: str,
    now: float,
    *,
    slug: str = "",
    end_ts: float = 0.0,
    source: str = "bag",
) -> dict:
    return {
        "condition_id": cid,
        "slug": slug,
        "up_token": up_token,
        "dn_token": dn_token,
        "end_ts": end_ts,
        "source": source,
        "status": "waiting",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
        "next_at": now,
        "tx_id": None,
        "zero_reads": 0,
    }
