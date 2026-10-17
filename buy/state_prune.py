"""Conservative, I/O-free pruning of terminal mint intents."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from buy.mint_loops import TERMINAL_PERSIST_STATUSES
from buy.mint_redeem import FINAL_STATUSES
from buy.mint_sequence import seq_exit_open


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _old(intent: dict, job: dict | None, now: float, retention: float) -> bool:
    base = [_number(intent.get(key)) for key in ("end_ts", "updated_at")]
    stamps = [value for value in base if value is not None and value > 0]
    if not stamps:
        return False
    job_updated = _number((job or {}).get("updated_at"))
    if job_updated is not None and job_updated > 0:
        stamps.append(job_updated)
    return now > max(stamps) + retention


def _settled(intent: dict, job: dict | None) -> bool:
    if intent.get("redeem_gave_up") or seq_exit_open(intent) or intent.get("sell_scrap_rest_id"):
        return False
    for key, value in intent.items():
        if value and key.endswith(("_inflight", "_uncertain", "_pending", "_retrying")):
            return False
    return not (
        intent.get("status") == "failed"
        and intent.get("relayer_state") in ("STATE_NEW", "STATE_EXECUTED", "STATE_MINED")
    )


def prune_state(state: Any, now: float, retention_s: float = 21600) -> list[dict]:
    """Remove safe old completed/failed intents and return detached archive rows.

    Caller holds the state lock and persists the mutation. Rows are returned
    for a separate archive writer; this function performs no I/O. Redeem jobs
    and all other state are preserved. Records without a usable end/update timestamp are kept.
    """
    moment = _number(now)
    retention = _number(retention_s)
    if moment is None or retention is None or retention < 0 or not isinstance(state, dict):
        return []
    intents = state.get("intents")
    jobs = state.get("redeems", {})
    if not isinstance(intents, dict) or not isinstance(jobs, dict):
        return []
    rows = []
    for cid, intent in list(intents.items()):
        if not isinstance(intent, dict) or intent.get("status") not in TERMINAL_PERSIST_STATUSES:
            continue
        job = jobs.get(cid)
        if cid in jobs and (not isinstance(job, dict) or job.get("status") not in FINAL_STATUSES):
            continue
        if not _old(intent, job, moment, retention) or not _settled(intent, job):
            continue
        rows.append({"ts": moment, "event": "state_pruned", "condition_id": cid, "intent": deepcopy(intent)})
        del intents[cid]
    return rows
