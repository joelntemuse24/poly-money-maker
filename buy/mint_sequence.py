"""Sequential mint: one bag of capital at a time (opt-in ``mint_sequential``).

Default mint books the next 15m window ~``enter_max_ttm_min`` ahead, so two
bags overlap and tie up two bags of pUSD. With ``mint_sequential`` the next
window is minted only inside ``[start - mint_seq_lead_s, start +
mint_seq_cutoff_s]`` and only once no other bag is still live and uncashed.
A bag stops blocking when its winner is cashed (``sold_winner``, which a
held dump also sets) or its window ends (cash then comes back by redeem).
Short pUSD inside the range is a wait, retried every mint tick. Past the
cutoff the window is skipped and logged once.

Minting is price neutral (pUSD split into Up + Down), so a mint at or just
after the open costs the same as one 14 minutes earlier. Sell, scrap, and
dump key off ``end_ts`` and the books, not on when the bag was minted.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

SEQ_LEAD_S = 30.0
SEQ_CUTOFF_S = 240.0
SEQ_WAIT_LOG_S = 30.0


def _num(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def seq_settings(cfg: Any) -> tuple[bool, float, float]:
    """``(enabled, lead_s, cutoff_s)`` from the strategy."""
    cfg = cfg or {}
    enabled = bool(cfg.get("mint_sequential", False))
    lead = max(0.0, _num(cfg.get("mint_seq_lead_s", SEQ_LEAD_S), SEQ_LEAD_S))
    cutoff = max(0.0, _num(cfg.get("mint_seq_cutoff_s", SEQ_CUTOFF_S), SEQ_CUTOFF_S))
    return enabled, lead, cutoff


def validate_seq(cfg: Any) -> None:
    cfg = cfg or {}
    for key in ("mint_seq_lead_s", "mint_seq_cutoff_s"):
        raw = cfg.get(key)
        if raw is None:
            continue
        value = _num(raw, -1.0)
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
    cutoff = _num(cfg.get("mint_seq_cutoff_s", SEQ_CUTOFF_S), SEQ_CUTOFF_S)
    if cutoff >= 600:
        raise ValueError("mint_seq_cutoff_s must be < 600")


def seq_phase(now: float, start_ts: float, lead_s: float, cutoff_s: float) -> str:
    """``early`` before ``start - lead``, ``late`` after ``start + cutoff``, else ``open``."""
    start = float(start_ts)
    if float(now) < start - float(lead_s):
        return "early"
    if float(now) > start + float(cutoff_s):
        return "late"
    return "open"


def seq_eligible_markets(markets: Iterable[Any], cfg: Any, now: float) -> list:
    """Markets in their sequential mint range, soonest first."""
    _enabled, lead, cutoff = seq_settings(cfg)
    require_accepting = bool((cfg or {}).get("require_accepting_orders"))
    out = []
    for market in markets:
        start = _num(getattr(market, "start_ts", 0), 0.0)
        end = _num(getattr(market, "end_ts", 0), 0.0)
        if start <= 0 or (end and now >= end):
            continue
        if seq_phase(now, start, lead, cutoff) != "open":
            continue
        if not getattr(market, "active", False) or getattr(market, "closed", False):
            continue
        if getattr(market, "neg_risk", False):
            continue
        if require_accepting and not getattr(market, "accepting_orders", False):
            continue
        out.append(market)
    return sorted(out, key=lambda m: float(m.start_ts))


def seq_late_markets(markets: Iterable[Any], cfg: Any, now: float) -> list:
    """Live windows past their cutoff (candidates for one ``mint_seq_skip`` line)."""
    _enabled, lead, cutoff = seq_settings(cfg)
    out = []
    for market in markets:
        start = _num(getattr(market, "start_ts", 0), 0.0)
        end = _num(getattr(market, "end_ts", 0), 0.0)
        if start <= 0 or not end or now >= end:
            continue
        if seq_phase(now, start, lead, cutoff) == "late":
            out.append(market)
    return out


def seq_busy_bag(
    state: Any,
    now: float,
    active_statuses: Iterable[str],
    *,
    exclude: str = "",
) -> Optional[tuple[str, dict]]:
    """The live bag still holding capital, if any.

    Live means an active (in-flight or confirmed) non-dry intent whose
    window has not ended and whose winner is not cashed. ``exclude`` is
    the candidate itself.
    """
    statuses = frozenset(active_statuses)
    intents = (state or {}).get("intents") or {}
    if not isinstance(intents, dict):
        return None
    busy: Optional[tuple[str, dict]] = None
    for cid, intent in intents.items():
        if not isinstance(intent, dict) or str(cid) == str(exclude):
            continue
        if str(intent.get("status") or "") not in statuses or intent.get("dry_run"):
            continue
        end = _num(intent.get("end_ts"), 0.0)
        if end and float(now) >= end:
            continue
        if intent.get("sold_winner"):
            continue
        if busy is None or _num(intent.get("start_ts"), 0.0) < _num(busy[1].get("start_ts"), 0.0):
            busy = (str(cid), intent)
    return busy


class SeqWaits:
    """In-memory wait bookkeeping: throttled wait lines and one skip per window."""

    def __init__(self, log_every_s: float = SEQ_WAIT_LOG_S) -> None:
        self.log_every_s = float(log_every_s)
        self._waits: dict[str, dict] = {}
        self._skipped: set[str] = set()

    def note_wait(self, cid: str, reason: str, now: float) -> bool:
        """Record a wait. True when a wait line is due (first, reason change, or throttle)."""
        row = self._waits.get(cid)
        if row is None:
            self._waits[cid] = {"reason": reason, "first": now, "logged": now, "n": 1}
            return True
        row["n"] += 1
        due = row["reason"] != reason or now - row["logged"] >= self.log_every_s
        row["reason"] = reason
        if due:
            row["logged"] = now
        return due

    def waited_s(self, cid: str, now: float) -> float:
        row = self._waits.get(cid)
        return 0.0 if row is None else max(0.0, now - row["first"])

    def minted(self, cid: str) -> None:
        self._waits.pop(cid, None)

    def take_skip(self, cid: str) -> Optional[dict]:
        """Wait record for a skipped window, once. ``{}`` if it was never waited on."""
        if cid in self._skipped:
            return None
        self._skipped.add(cid)
        if len(self._skipped) > 500:
            self._skipped = set(list(self._skipped)[-200:])
        return self._waits.pop(cid, None) or {}
