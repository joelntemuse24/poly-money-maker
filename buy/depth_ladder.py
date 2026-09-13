"""Ask-side depth ladder for research telemetry (no trading).

Given ask levels and a FAK buy limit, report how much size is available for
fixed dollar budgets. Used by the hourly and 15m buy bots to log book depth
on every order-path attempt / fill without changing trade sizes.

Also provides a short ring-buffer + ``simulate_topup_path`` so research can ask:
if the single-tick max fill was only $18 against a $50 target, would topping up
on later ticks (while entry gates still hold) have completed $20/$32/$40/$50?
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

DEFAULT_BUDGETS: Tuple[float, ...] = (5.0, 10.0, 20.0, 32.0, 40.0, 50.0, 100.0)
DEFAULT_TOPUP_TARGETS: Tuple[float, ...] = (20.0, 32.0, 40.0, 50.0)
DEFAULT_PATH_BUFFER_MAX = 120

TOPUP_ASSUMPTION = (
    "one take per sample interval of then-visible ask depth ≤ limit; "
    "only samples with gates_ok=True count; does not model queue priority, "
    "competing takers, or replenishment within the same sample"
)

Level = Tuple[float, float]


def _finite(value: Any, *, minimum: Optional[float] = None, maximum: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def normalize_asks(asks: Any) -> List[Level]:
    """Return ascending (price, size) ask levels with 0 < price < 1 and size > 0."""
    valid: List[Level] = []
    for level in asks or []:
        if isinstance(level, Mapping):
            price = _finite(level.get("price"), minimum=0, maximum=1)
            size = _finite(level.get("size"), minimum=0)
        elif isinstance(level, (tuple, list)) and len(level) >= 2:
            price = _finite(level[0], minimum=0, maximum=1)
            size = _finite(level[1], minimum=0)
        else:
            continue
        if price is None or size is None or not 0 < price < 1 or size <= 0:
            continue
        valid.append((price, size))
    valid.sort(key=lambda item: item[0])
    return valid


def summarize_ask_levels(asks: Any, n: int = 5) -> List[Dict[str, float]]:
    """Top-n ask levels as compact {p,s} dicts (ascending price)."""
    out: List[Dict[str, float]] = []
    for price, size in normalize_asks(asks)[: max(0, int(n))]:
        out.append({"p": round(price, 4), "s": round(size, 4)})
    return out


def _walk_fill(
    levels: Sequence[Level],
    limit_price: float,
    budget: float,
) -> Dict[str, Any]:
    """Simulate a buy spending up to ``budget`` at prices <= ``limit_price``."""
    rem = float(budget)
    shares = 0.0
    notional = 0.0
    for price, size in levels:
        if price > limit_price + 1e-12:
            break
        if rem <= 1e-12:
            break
        level_notional = price * size
        if level_notional <= rem + 1e-12:
            take = size
            cost = level_notional
        else:
            take = rem / price
            cost = rem
        shares += take
        notional += cost
        rem -= cost
    available_shares = 0.0
    available_notional = 0.0
    for price, size in levels:
        if price > limit_price + 1e-12:
            break
        available_shares += size
        available_notional += price * size

    if available_notional <= 1e-12:
        status = "zero"
    elif available_notional + 1e-9 >= float(budget):
        status = "full"
    else:
        status = "partial"

    vwap = None
    if shares > 1e-12 and notional > 1e-12:
        vwap = round(notional / shares, 6)

    return {
        "shares": round(shares, 6),
        "notional": round(notional, 6),
        "available_shares": round(available_shares, 6),
        "available_notional": round(available_notional, 6),
        "fill_status": status,
        "vwap": vwap,
    }


def compute_depth_ladder(
    asks: Any,
    limit_price: float,
    budgets: Iterable[float] = DEFAULT_BUDGETS,
) -> Dict[str, Any]:
    """Build per-budget depth map for a FAK buy at ``limit_price``.

    Returns::
        {
          "available_shares": float,
          "available_notional": float,
          "budgets": {
            "5": {"shares", "notional", "available_shares", "available_notional",
                  "fill_status", "vwap"},
            ...
          },
          "top_asks": [{"p", "s"}, ...],
        }
    """
    limit = _finite(limit_price, minimum=0, maximum=1)
    levels = normalize_asks(asks)
    if limit is None:
        limit = 0.0
        usable: List[Level] = []
    else:
        usable = [(p, s) for p, s in levels if p <= limit + 1e-12]

    avail_shares = sum(s for _, s in usable)
    avail_notional = sum(p * s for p, s in usable)

    budget_map: Dict[str, Any] = {}
    for raw in budgets:
        b = _finite(raw, minimum=0)
        if b is None or b <= 0:
            continue
        key = str(int(b)) if float(b).is_integer() else str(b)
        budget_map[key] = _walk_fill(usable, limit, b)

    # Headline: if we sized up, how much would we actually get?
    max_vwap = None
    if avail_shares > 1e-12 and avail_notional > 1e-12:
        max_vwap = round(avail_notional / avail_shares, 6)
    clip_budget = None
    for key, row in budget_map.items():
        if row.get("fill_status") != "full":
            clip_budget = key
            break
    if avail_notional <= 1e-12:
        scale_summary = "max_fill=$0 (no asks <= limit)"
    elif clip_budget is None:
        scale_summary = (
            f"max_fill=${avail_notional:.2f} ({avail_shares:.2f} sh @~{max_vwap}); "
            f"all listed budgets full"
        )
    else:
        scale_summary = (
            f"max_fill=${avail_notional:.2f} ({avail_shares:.2f} sh @~{max_vwap}); "
            f"${clip_budget}+ would be partial/zero"
        )

    return {
        "available_shares": round(avail_shares, 6),
        "available_notional": round(avail_notional, 6),
        "max_fill_shares": round(avail_shares, 6),
        "max_fill_usd": round(avail_notional, 6),
        "max_fill_vwap": max_vwap,
        "clips_above_usd": float(clip_budget) if clip_budget is not None else None,
        "scale_summary": scale_summary,
        "budgets": budget_map,
        "top_asks": summarize_ask_levels(usable, n=5),
    }


def available_at_limit(
    asks: Any,
    limit_price: float,
    *,
    best_ask: Any = None,
    best_ask_size: Any = None,
) -> Dict[str, Any]:
    """Available shares/notional ≤ limit from full asks, else TOB fallback.

    TOB fallback uses only the displayed best ask size when ask ≤ limit.
    """
    limit = _finite(limit_price, minimum=0, maximum=1)
    levels = normalize_asks(asks)
    if limit is not None and levels:
        usable = [(p, s) for p, s in levels if p <= limit + 1e-12]
        avail_shares = sum(s for _, s in usable)
        avail_notional = sum(p * s for p, s in usable)
        best = usable[0][0] if usable else None
        return {
            "available_shares": round(avail_shares, 6),
            "available_notional": round(avail_notional, 6),
            "best_ask": round(best, 6) if best is not None else None,
            "source": "levels",
        }
    ask = _finite(best_ask, minimum=0, maximum=1)
    size = _finite(best_ask_size, minimum=0)
    if (
        limit is not None
        and ask is not None
        and size is not None
        and size > 0
        and ask <= limit + 1e-12
    ):
        notional = ask * size
        return {
            "available_shares": round(size, 6),
            "available_notional": round(notional, 6),
            "best_ask": round(ask, 6),
            "source": "tob",
        }
    return {
        "available_shares": 0.0,
        "available_notional": 0.0,
        "best_ask": round(ask, 6) if ask is not None else None,
        "source": "empty",
    }


def make_depth_sample(
    *,
    available_notional: float,
    available_shares: float = 0.0,
    best_ask: Optional[float] = None,
    gates_ok: bool = False,
    ttm: Optional[float] = None,
    limit: Optional[float] = None,
    ts_mono: Optional[float] = None,
    ts_wall: Optional[float] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """One ring-buffer sample for top-up path simulation."""
    return {
        "ts_mono": float(time.monotonic() if ts_mono is None else ts_mono),
        "ts_wall": float(time.time() if ts_wall is None else ts_wall),
        "available_notional": round(float(available_notional or 0.0), 6),
        "available_shares": round(float(available_shares or 0.0), 6),
        "best_ask": (
            round(float(best_ask), 6)
            if best_ask is not None and _finite(best_ask) is not None
            else None
        ),
        "gates_ok": bool(gates_ok),
        "ttm": round(float(ttm), 4) if ttm is not None and _finite(ttm) is not None else None,
        "limit": (
            round(float(limit), 4)
            if limit is not None and _finite(limit, minimum=0, maximum=1) is not None
            else None
        ),
        "source": source,
    }


class DepthPathBuffer:
    """Fixed-length ring buffer of depth samples for one condition|leg."""

    def __init__(self, maxlen: int = DEFAULT_PATH_BUFFER_MAX):
        self.maxlen = max(1, int(maxlen))
        self._samples: Deque[Dict[str, Any]] = deque(maxlen=self.maxlen)

    def append(self, sample: Mapping[str, Any]) -> None:
        self._samples.append(dict(sample))

    def extend(self, samples: Iterable[Mapping[str, Any]]) -> None:
        for sample in samples:
            self.append(sample)

    def clear(self) -> None:
        self._samples.clear()

    def __len__(self) -> int:
        return len(self._samples)

    def samples(self) -> List[Dict[str, Any]]:
        return list(self._samples)


def simulate_topup_path(
    samples: Sequence[Mapping[str, Any]],
    targets: Iterable[float] = DEFAULT_TOPUP_TARGETS,
    *,
    require_gates_ok: bool = True,
) -> Dict[str, Any]:
    """Walk time-ordered samples and accumulate fill toward dollar targets.

    Conservative model (see ``TOPUP_ASSUMPTION``): each sample may contribute at
    most that sample's ``available_notional`` once — we do not re-take the same
    resting liquidity forever across ticks.
    """
    ordered = sorted(
        (dict(s) for s in (samples or [])),
        key=lambda s: (
            float(s.get("ts_mono") or 0.0),
            float(s.get("ts_wall") or 0.0),
        ),
    )
    usable: List[Dict[str, Any]] = []
    for sample in ordered:
        if require_gates_ok and not bool(sample.get("gates_ok")):
            continue
        avail = _finite(sample.get("available_notional"), minimum=0) or 0.0
        if avail <= 1e-12:
            continue
        usable.append(sample)

    target_vals: List[float] = []
    for raw in targets:
        t = _finite(raw, minimum=0)
        if t is None or t <= 0:
            continue
        target_vals.append(float(t))

    max_single = 0.0
    for sample in usable:
        avail = float(sample.get("available_notional") or 0.0)
        if avail > max_single:
            max_single = avail

    target_out: Dict[str, Any] = {}
    for target in target_vals:
        key = str(int(target)) if float(target).is_integer() else str(target)
        remaining = float(target)
        cum = 0.0
        samples_used = 0
        seconds_to_complete: Optional[float] = None
        completed = False
        t0 = float(usable[0]["ts_mono"]) if usable else None
        for sample in usable:
            take = min(remaining, float(sample.get("available_notional") or 0.0))
            if take <= 1e-12:
                continue
            cum += take
            remaining -= take
            samples_used += 1
            if remaining <= 1e-9:
                completed = True
                t1 = float(sample.get("ts_mono") or 0.0)
                if t0 is not None:
                    seconds_to_complete = round(max(0.0, t1 - t0), 3)
                break
        target_out[key] = {
            "target_usd": float(target),
            "cum_fill_usd": round(cum, 6),
            "completed": bool(completed),
            "seconds_to_complete": seconds_to_complete,
            "samples_used": int(samples_used),
        }

    span_s = None
    if ordered:
        t_first = float(ordered[0].get("ts_mono") or 0.0)
        t_last = float(ordered[-1].get("ts_mono") or 0.0)
        span_s = round(max(0.0, t_last - t_first), 3)

    return {
        "targets": target_out,
        "max_single_sample_fill_usd": round(max_single, 6),
        "assumption": TOPUP_ASSUMPTION,
        "n_samples": len(ordered),
        "n_gates_ok": sum(1 for s in ordered if bool(s.get("gates_ok"))),
        "n_usable": len(usable),
        "buffer_span_s": span_s,
    }


def build_depth_ladder_event(
    asks: Any,
    limit_price: Any,
    *,
    budgets: Iterable[float] = DEFAULT_BUDGETS,
    ask: Any = None,
    condition_id: Any = None,
    token_id: Any = None,
    slug: Any = None,
    leg: Any = None,
    slice_name: Any = None,
    ttm: Any = None,
    attempt: Any = None,
    outcome: Any = None,
) -> Dict[str, Any]:
    """Build the ``buy_depth_ladder`` log payload (telemetry only)."""
    ladder = compute_depth_ladder(asks, limit_price, budgets=budgets)
    ask_f = _finite(ask, minimum=0, maximum=1)
    limit_f = _finite(limit_price, minimum=0, maximum=1)
    return {
        "condition_id": condition_id,
        "token_id": token_id,
        "slug": slug,
        "leg": leg,
        "slice": slice_name,
        "ask": round(float(ask_f), 4) if ask_f is not None else None,
        "limit": round(float(limit_f), 4) if limit_f is not None else None,
        "ttm": ttm,
        "attempt": attempt,
        "outcome": outcome,
        "available_shares": ladder.get("available_shares"),
        "available_notional": ladder.get("available_notional"),
        "max_fill_shares": ladder.get("max_fill_shares"),
        "max_fill_usd": ladder.get("max_fill_usd"),
        "max_fill_vwap": ladder.get("max_fill_vwap"),
        "clips_above_usd": ladder.get("clips_above_usd"),
        "scale_summary": ladder.get("scale_summary"),
        "budgets": ladder.get("budgets"),
        "top_asks": ladder.get("top_asks") or summarize_ask_levels(asks, n=5),
    }


def emit_depth_ladder_event(
    *,
    log_event: Any,
    jsonl_path: str,
    asks: Any,
    limit_price: Any,
    budgets: Iterable[float] = DEFAULT_BUDGETS,
    ask: Any = None,
    condition_id: Any = None,
    token_id: Any = None,
    slug: Any = None,
    leg: Any = None,
    slice_name: Any = None,
    ttm: Any = None,
    attempt: Any = None,
    outcome: Any = None,
) -> Optional[Dict[str, Any]]:
    """Compute, log, and append one ``buy_depth_ladder`` event.

    Failures are swallowed (telemetry must never block a buy).
    """
    try:
        payload = build_depth_ladder_event(
            asks,
            limit_price,
            budgets=budgets,
            ask=ask,
            condition_id=condition_id,
            token_id=token_id,
            slug=slug,
            leg=leg,
            slice_name=slice_name,
            ttm=ttm,
            attempt=attempt,
            outcome=outcome,
        )
        if log_event is not None:
            log_event("buy_depth_ladder", **{
                k: v for k, v in payload.items() if v is not None
            })
        if jsonl_path:
            append_depth_ladder_jsonl(jsonl_path, {
                "event": "buy_depth_ladder",
                **payload,
            })
        return payload
    except Exception as exc:
        try:
            if log_event is not None:
                log_event(
                    "buy_depth_ladder_fail",
                    token_id=token_id,
                    error=str(exc)[:200],
                )
        except Exception:
            pass
        return None


class DepthPathTracker:
    """Per-leg ring buffer + top-up path sim (research only)."""

    def __init__(
        self,
        *,
        jsonl_path: str,
        targets: Iterable[float] = DEFAULT_TOPUP_TARGETS,
        maxlen: int = DEFAULT_PATH_BUFFER_MAX,
        sample_min_interval_s: float = 1.0,
        emit_min_interval_s: float = 12.0,
    ):
        self.jsonl_path = jsonl_path
        self.targets = tuple(targets)
        self.maxlen = max(1, int(maxlen))
        self.sample_min_interval_s = float(sample_min_interval_s)
        self.emit_min_interval_s = float(emit_min_interval_s)
        self._buffers: Dict[str, DepthPathBuffer] = {}
        self._last_sample_mono: Dict[str, float] = {}
        self._last_emit_mono: Dict[str, float] = {}

    @staticmethod
    def _key(condition_id=None, leg=None, token_id=None) -> Optional[str]:
        if condition_id and leg:
            return f"{condition_id}|{leg}"
        if token_id:
            return str(token_id)
        return None

    def _buffer(self, key: str) -> DepthPathBuffer:
        buf = self._buffers.get(key)
        if buf is None:
            buf = DepthPathBuffer(maxlen=self.maxlen)
            self._buffers[key] = buf
        return buf

    def prune(self, keep_tokens=None) -> None:
        keep = {str(t) for t in (keep_tokens or []) if t}
        if not keep:
            if len(self._buffers) > 64:
                for stale in list(self._buffers.keys())[:32]:
                    self._buffers.pop(stale, None)
                    self._last_sample_mono.pop(stale, None)
                    self._last_emit_mono.pop(stale, None)
            return
        for key in list(self._buffers.keys()):
            if "|" not in key and key not in keep:
                self._buffers.pop(key, None)
                self._last_sample_mono.pop(key, None)
                self._last_emit_mono.pop(key, None)
        if len(self._buffers) > 128:
            ordered = sorted(
                self._buffers.items(),
                key=lambda kv: self._last_sample_mono.get(kv[0], 0.0),
            )
            for stale, _buf in ordered[:40]:
                self._buffers.pop(stale, None)
                self._last_sample_mono.pop(stale, None)
                self._last_emit_mono.pop(stale, None)

    def record_sample(
        self,
        *,
        available_notional: float,
        available_shares: float = 0.0,
        best_ask: Optional[float] = None,
        gates_ok: bool = False,
        ttm: Optional[float] = None,
        limit: Optional[float] = None,
        source: Optional[str] = None,
        condition_id=None,
        token_id=None,
        leg=None,
        slice_name=None,
        force: bool = False,
        min_interval_s: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        key = self._key(condition_id, leg, token_id)
        if not key:
            return None
        now_mono = time.monotonic()
        interval = self.sample_min_interval_s if min_interval_s is None else float(min_interval_s)
        if not force:
            prev = self._last_sample_mono.get(key, 0.0)
            if now_mono - prev < interval:
                return None
        sample = make_depth_sample(
            available_notional=available_notional,
            available_shares=available_shares,
            best_ask=best_ask,
            gates_ok=bool(gates_ok),
            ttm=ttm,
            limit=limit,
            ts_mono=now_mono,
            source=source,
        )
        if slice_name:
            sample["slice"] = slice_name
        self._buffer(key).append(sample)
        self._last_sample_mono[key] = now_mono
        return sample

    def emit_topup(
        self,
        *,
        log_event: Any,
        condition_id=None,
        token_id=None,
        slug=None,
        leg=None,
        slice_name=None,
        limit_price=None,
        ttm=None,
        outcome=None,
        force: bool = False,
        targets: Optional[Iterable[float]] = None,
        min_interval_s: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        key = self._key(condition_id, leg, token_id)
        if not key:
            return None
        buf = self._buffers.get(key)
        if buf is None or len(buf) < 2:
            return None
        now_mono = time.monotonic()
        interval = self.emit_min_interval_s if min_interval_s is None else float(min_interval_s)
        if not force:
            prev = self._last_emit_mono.get(key, 0.0)
            if now_mono - prev < interval:
                return None
        samples = buf.samples()
        use_targets = self.targets if targets is None else targets
        sim = simulate_topup_path(samples, targets=use_targets)
        limit = _finite(limit_price, minimum=0, maximum=1)
        if limit is None and samples:
            limit = _finite(samples[-1].get("limit"), minimum=0, maximum=1)
        payload = {
            "condition_id": condition_id,
            "token_id": token_id,
            "slug": slug,
            "leg": leg,
            "slice": slice_name,
            "limit": round(float(limit), 4) if limit is not None else None,
            "ttm": ttm,
            "outcome": outcome,
            "targets": sim.get("targets"),
            "max_single_sample_fill_usd": sim.get("max_single_sample_fill_usd"),
            "assumption": sim.get("assumption") or TOPUP_ASSUMPTION,
            "buffer_span_s": sim.get("buffer_span_s"),
            "n_samples": sim.get("n_samples"),
            "n_gates_ok": sim.get("n_gates_ok"),
            "n_usable": sim.get("n_usable"),
        }
        if log_event is not None:
            log_event("buy_depth_topup_sim", **{
                k: v for k, v in payload.items() if v is not None
            })
        if self.jsonl_path:
            append_depth_ladder_jsonl(self.jsonl_path, {
                "event": "buy_depth_topup_sim",
                **payload,
            })
        self._last_emit_mono[key] = now_mono
        return payload


def append_depth_ladder_jsonl(path: str, record: Mapping[str, Any]) -> None:
    """Append one JSON line. Failures are swallowed (telemetry only)."""
    if not path:
        return
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        row = dict(record)
        row.setdefault("logged_at", time.time())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        pass


def self_test() -> None:
    """Synthetic deep vs thin books + top-up path; raise on mismatch."""
    deep = [(0.34, 100.0), (0.35, 200.0), (0.36, 500.0)]
    thin = [(0.34, 2.0), (0.90, 1.0)]
    deep_out = compute_depth_ladder(deep, limit_price=0.40, budgets=(5, 20, 50, 100))
    thin_out = compute_depth_ladder(thin, limit_price=0.40, budgets=(5, 20, 50, 100))

    assert deep_out["budgets"]["5"]["fill_status"] == "full", deep_out
    assert deep_out["budgets"]["100"]["fill_status"] == "full", deep_out
    assert abs(deep_out["budgets"]["5"]["vwap"] - 0.34) < 1e-6, deep_out
    assert thin_out["budgets"]["5"]["fill_status"] == "partial", thin_out
    assert thin_out["budgets"]["20"]["fill_status"] == "partial", thin_out
    assert thin_out["available_notional"] < 5.0, thin_out
    zero = compute_depth_ladder([], limit_price=0.99, budgets=(5,))
    assert zero["budgets"]["5"]["fill_status"] == "zero", zero
    # Levels above limit ignored
    capped = compute_depth_ladder([(0.50, 10.0), (0.95, 1000.0)], limit_price=0.60)
    assert capped["available_shares"] == 10.0, capped

    # Top-up path: $18 then $20 gates_ok → $50 not complete in 2 samples
    t0 = 1000.0
    path_partial = [
        make_depth_sample(
            available_notional=18.0, available_shares=20.0, best_ask=0.90,
            gates_ok=True, limit=0.99, ts_mono=t0, ts_wall=t0,
        ),
        make_depth_sample(
            available_notional=20.0, available_shares=22.0, best_ask=0.91,
            gates_ok=True, limit=0.99, ts_mono=t0 + 1.0, ts_wall=t0 + 1.0,
        ),
    ]
    sim_partial = simulate_topup_path(path_partial, targets=(20, 32, 40, 50))
    assert sim_partial["targets"]["20"]["completed"] is True, sim_partial
    assert sim_partial["targets"]["20"]["samples_used"] == 2, sim_partial
    assert sim_partial["targets"]["50"]["completed"] is False, sim_partial
    assert abs(sim_partial["targets"]["50"]["cum_fill_usd"] - 38.0) < 1e-6, sim_partial
    assert sim_partial["max_single_sample_fill_usd"] == 20.0, sim_partial

    # gates_ok=False sample must not count
    gated = list(path_partial)
    gated.append(
        make_depth_sample(
            available_notional=50.0, available_shares=50.0, best_ask=0.90,
            gates_ok=False, limit=0.99, ts_mono=t0 + 2.0, ts_wall=t0 + 2.0,
        )
    )
    sim_gated = simulate_topup_path(gated, targets=(50,))
    assert sim_gated["targets"]["50"]["completed"] is False, sim_gated
    assert abs(sim_gated["targets"]["50"]["cum_fill_usd"] - 38.0) < 1e-6, sim_gated

    # 3×$20 → $50 completes in 3 samples
    path_full = [
        make_depth_sample(
            available_notional=20.0, available_shares=22.0, best_ask=0.90,
            gates_ok=True, limit=0.99, ts_mono=t0 + i, ts_wall=t0 + i,
        )
        for i in range(3)
    ]
    sim_full = simulate_topup_path(path_full, targets=(20, 32, 40, 50))
    assert sim_full["targets"]["50"]["completed"] is True, sim_full
    assert sim_full["targets"]["50"]["samples_used"] == 3, sim_full
    assert sim_full["targets"]["50"]["seconds_to_complete"] == 2.0, sim_full
    assert abs(sim_full["targets"]["50"]["cum_fill_usd"] - 50.0) < 1e-6, sim_full
    assert sim_full["assumption"] == TOPUP_ASSUMPTION

    buf = DepthPathBuffer(maxlen=2)
    buf.append(path_partial[0])
    buf.append(path_partial[1])
    buf.append(path_full[0])
    assert len(buf) == 2
    assert buf.samples()[0]["available_notional"] == 20.0

    tob = available_at_limit([], 0.99, best_ask=0.90, best_ask_size=10.0)
    assert abs(tob["available_notional"] - 9.0) < 1e-9, tob
    assert tob["source"] == "tob", tob

    print("depth_ladder self_test OK", json.dumps({
        "deep_5": deep_out["budgets"]["5"],
        "thin_5": thin_out["budgets"]["5"],
        "deep_avail": deep_out["available_notional"],
        "thin_avail": thin_out["available_notional"],
        "topup_partial_50": sim_partial["targets"]["50"],
        "topup_full_50": sim_full["targets"]["50"],
    }))


if __name__ == "__main__":
    self_test()
