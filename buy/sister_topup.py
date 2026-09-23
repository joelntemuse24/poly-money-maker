"""When wallet B cannot fund a hedge, move $5 of pUSD from A once.

Pure policy. No keys, no RPC, no relayer. ``sister_topup.py`` submits the
PROXY batch. One transfer per broke episode: after it is accepted, another
waits until B's balance is back above the need (or, when the top-up was
because a place failed while the balance already looked funded, until a
later place clears).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from buy.sister_bid import COMPLEMENT_DEPOSIT, COMPLEMENT_PROXY, MINTBOT_FUNDER

PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a ``.env`` file. Values stay in the returned dict; nothing is logged."""
    out: dict[str, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def fresh_topup_state() -> dict:
    return {
        "episode_open": False,
        "close_requires_clear": False,
        "last_attempt_at": 0.0,
        "last_result": "",
        "last_tx": "",
        "last_balance": None,
        "last_reason": "",
        "last_logged_reason": "",
    }


def recipient_ok(addr: str) -> tuple[bool, str]:
    """B's deposit wallet only. Never A's funder or the Magic proxy."""
    text = str(addr or "").strip()
    if not text:
        return False, "missing_recipient"
    low = text.lower()
    if low == MINTBOT_FUNDER.lower():
        return False, "mintbot_funder"
    if low == COMPLEMENT_PROXY.lower():
        return False, "magic_proxy"
    if low != COMPLEMENT_DEPOSIT.lower():
        return False, "not_deposit_wallet"
    return True, "ok"


def place_error_is_broke(text: str) -> bool:
    """True when a CLOB place failure is a balance or allowance miss."""
    low = str(text or "").lower()
    if not low:
        return False
    needles = (
        "not enough balance",
        "not_enough_balance",
        "enough_balance",
        "insufficient balance",
        "insufficient funds",
        "balance is not enough",
        "not enough allowance",
        "insufficient allowance",
        "allowance",
    )
    return any(needle in low for needle in needles)


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _copy_state(state: Optional[dict]) -> dict:
    base = fresh_topup_state()
    if isinstance(state, dict):
        base.update(state)
    base["episode_open"] = bool(base.get("episode_open"))
    base["close_requires_clear"] = bool(base.get("close_requires_clear"))
    attempt = _finite(base.get("last_attempt_at"))
    base["last_attempt_at"] = attempt if attempt is not None else 0.0
    balance = _finite(base.get("last_balance"))
    base["last_balance"] = balance
    return base


def topup_decision(
    state: Optional[dict],
    *,
    now_s: float,
    balance: Any,
    need_usd: float,
    amount_usd: float,
    enabled: bool,
    dry_run: bool,
    force_broke: bool,
    cleared: bool,
    recipient: str,
    a_balance: Any = None,
    retry_s: float = 120.0,
) -> tuple[dict, dict]:
    """``(state, decision)``. ``decision["action"]`` is skip, dry, or transfer.

    Does not submit. A transfer decision leaves the episode closed until
    ``note_topup_result`` records an accepted tx.
    """
    current = _copy_state(state)
    need = float(need_usd or 0)
    amount = float(amount_usd or 0)
    now = float(now_s)
    bal = _finite(balance)
    short = bal is not None and need > 0 and bal + 1e-9 < need
    decision = {
        "action": "skip",
        "reason": "funded",
        "amount_usd": amount,
        "recipient": str(recipient or ""),
        "balance": bal,
        "close_requires_clear": False,
    }

    def _skip(reason: str) -> tuple[dict, dict]:
        decision["action"] = "skip"
        decision["reason"] = reason
        current["last_reason"] = reason
        current["last_balance"] = bal
        return current, decision

    if not enabled:
        return _skip("disabled")
    if amount <= 0 or need <= 0:
        return _skip("bad_amount")
    ok, why = recipient_ok(recipient)
    if not ok:
        return _skip(why)

    if current["episode_open"] and not force_broke:
        if current["close_requires_clear"]:
            if cleared:
                current["episode_open"] = False
                current["close_requires_clear"] = False
                return _skip("cleared")
            return _skip("episode_open")
        if bal is not None and bal + 1e-9 >= need:
            current["episode_open"] = False
            current["close_requires_clear"] = False
            return _skip("recovered")
        return _skip("episode_open")

    if current["episode_open"] and force_broke:
        return _skip("episode_open")

    broke = short or bool(force_broke)
    if not broke:
        return _skip("funded")

    retry = max(0.0, float(retry_s or 0))
    if (
        current.get("last_result") == "fail"
        and now + 1e-12 < float(current["last_attempt_at"]) + retry
    ):
        return _skip("backoff")

    a_bal = _finite(a_balance)
    if not dry_run and a_bal is None:
        return _skip("a_unknown")
    if a_bal is not None and a_bal + 1e-9 < amount:
        return _skip("a_short")

    reason = "place_fail" if force_broke and not short else "b_short"
    if bal is None and force_broke:
        reason = "place_fail"
    decision["reason"] = reason
    decision["close_requires_clear"] = bool(force_broke and not short)
    current["last_reason"] = reason
    current["last_balance"] = bal
    if dry_run:
        if (
            current.get("last_result") == "dry"
            and now + 1e-12 < float(current["last_attempt_at"]) + retry
        ):
            return _skip("dry_throttled")
        current["last_result"] = "dry"
        current["last_attempt_at"] = now
        decision["action"] = "dry"
        return current, decision
    decision["action"] = "transfer"
    return current, decision


def note_topup_result(
    state: Optional[dict],
    *,
    now_s: float,
    ok: bool,
    tx_id: str = "",
    error: str = "",
    close_requires_clear: bool = False,
) -> dict:
    """Record a submit attempt. An accepted tx opens the episode."""
    current = _copy_state(state)
    current["last_attempt_at"] = float(now_s)
    if ok:
        current["episode_open"] = True
        current["close_requires_clear"] = bool(close_requires_clear)
        current["last_result"] = "ok"
        current["last_tx"] = str(tx_id or "")
        current["last_reason"] = "submitted"
        return current
    current["episode_open"] = False
    current["close_requires_clear"] = False
    current["last_result"] = "fail"
    current["last_tx"] = ""
    current["last_reason"] = "submit_fail"
    current["last_error"] = str(error or "")[:200]
    return current


def apply_topup(
    state: Optional[dict],
    *,
    now_s: float,
    balance: Any,
    need_usd: float,
    amount_usd: float,
    enabled: bool,
    dry_run: bool,
    force_broke: bool,
    cleared: bool,
    recipient: str,
    a_balance: Any = None,
    retry_s: float = 120.0,
    submitter: Optional[Callable[[], tuple[bool, str, str]]] = None,
) -> tuple[dict, dict]:
    """Decide, and submit only when the decision is a live transfer."""
    current, decision = topup_decision(
        state,
        now_s=now_s,
        balance=balance,
        need_usd=need_usd,
        amount_usd=amount_usd,
        enabled=enabled,
        dry_run=dry_run,
        force_broke=force_broke,
        cleared=cleared,
        recipient=recipient,
        a_balance=a_balance,
        retry_s=retry_s,
    )
    if decision["action"] != "transfer":
        return current, decision
    if submitter is None:
        current = note_topup_result(
            current,
            now_s=now_s,
            ok=False,
            error="no_submitter",
        )
        decision["action"] = "skip"
        decision["reason"] = "no_submitter"
        return current, decision
    try:
        ok, tx_id, error = submitter()
    except Exception as exc:
        ok, tx_id, error = False, "", str(exc)[:200]
    current = note_topup_result(
        current,
        now_s=now_s,
        ok=bool(ok),
        tx_id=str(tx_id or ""),
        error=str(error or ""),
        close_requires_clear=bool(decision.get("close_requires_clear")),
    )
    if not ok:
        decision["action"] = "skip"
        decision["reason"] = "submit_fail"
        decision["error"] = str(error or "")[:200]
    else:
        decision["tx_id"] = str(tx_id or "")
    return current, decision
