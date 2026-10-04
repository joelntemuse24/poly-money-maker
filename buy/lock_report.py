"""Running P&L from ``logs/lockbot.jsonl`` settlement rows."""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional


def _num(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def load_jsonl(path: Any) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def summarize(rows: Iterable[dict]) -> dict:
    """Settled P&L by strategy (s1 / s2), lane, and asset.

    One settlement can share a slug across strategies and sides, so the
    key is slug + strategy + side. Win rate uses the ``won`` flag.
    Signal and paper-fill counts are included so a dry-run day can be
    read before every window has settled. Latency percentiles come from
    decision/post/ack stamps on those attempts.
    """
    from buy.lock_paper import latency_summary

    stored = list(rows or [])
    by_key: dict[str, dict] = {}
    signals = {"s1": 0, "s2": 0}
    fills = {
        "s1": {"n": 0, "cost": 0.0, "shares": 0.0},
        "s2": {"n": 0, "cost": 0.0, "shares": 0.0},
    }
    for row in stored:
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        strategy = str(row.get("strategy") or "")
        if strategy not in {"s1", "s2"}:
            strategy = ""
        if event == "signal" and strategy:
            signals[strategy] += 1
        if event in {"paper_fill", "entry"} and strategy:
            shares = _num(row.get("shares")) or 0.0
            if shares > 0:
                fills[strategy]["n"] += 1
                fills[strategy]["cost"] += _num(row.get("cost")) or 0.0
                fills[strategy]["shares"] += shares
        if event != "settlement":
            continue
        slug = str(row.get("slug") or "")
        if not slug:
            continue
        pnl = _num(row.get("pnl"))
        if pnl is None:
            continue
        key = f"{slug}|{row.get('strategy') or ''}|{row.get('side') or ''}"
        by_key[key] = row

    def bucket() -> dict:
        return {"markets": 0, "wins": 0, "pnl": 0.0, "worst": None, "worst_slug": None}

    lanes: dict[str, dict] = {}
    assets: dict[str, dict] = {}
    strategies: dict[str, dict] = {}
    overall = bucket()

    def add(into: dict, row: dict) -> None:
        pnl = float(row["pnl"])
        into["markets"] += 1
        into["pnl"] += pnl
        if bool(row.get("won")):
            into["wins"] += 1
        if into["worst"] is None or pnl < into["worst"]:
            into["worst"] = pnl
            into["worst_slug"] = row.get("slug")

    for row in by_key.values():
        lane = str(row.get("lane") or "ext")
        asset = str(row.get("asset") or "?")
        duration = str(row.get("duration") or "")
        asset_key = f"{asset}_{duration}" if duration else asset
        strategy = str(row.get("strategy") or "unset")
        add(overall, row)
        add(lanes.setdefault(lane, bucket()), row)
        add(assets.setdefault(asset_key, bucket()), row)
        add(strategies.setdefault(strategy, bucket()), row)

    def finish(raw: dict) -> dict:
        n = int(raw["markets"])
        out = dict(raw)
        out["pnl"] = round(float(raw["pnl"]), 4)
        out["win_rate"] = (raw["wins"] / n) if n else None
        if raw["worst"] is not None:
            out["worst"] = round(float(raw["worst"]), 4)
        return out

    return {
        "markets": overall["markets"],
        "pnl": round(overall["pnl"], 4),
        "win_rate": (overall["wins"] / overall["markets"]) if overall["markets"] else None,
        "worst": None if overall["worst"] is None else round(float(overall["worst"]), 4),
        "worst_slug": overall["worst_slug"],
        "lanes": {key: finish(value) for key, value in sorted(lanes.items())},
        "assets": {key: finish(value) for key, value in sorted(assets.items())},
        "strategies": {key: finish(value) for key, value in sorted(strategies.items())},
        "signals": signals,
        "fills": {
            key: {"n": row["n"], "cost": round(row["cost"], 4), "shares": round(row["shares"], 4)}
            for key, row in fills.items()
        },
        "latency": latency_summary(stored),
    }


def format_summary(summary: dict) -> str:
    lines = [
        f"markets {summary.get('markets', 0)}  pnl ${float(summary.get('pnl') or 0):.2f}  "
        f"win_rate {_pct(summary.get('win_rate'))}  "
        f"worst ${float(summary['worst']):.2f} ({summary.get('worst_slug')})"
        if summary.get("worst") is not None
        else f"markets {summary.get('markets', 0)}  pnl ${float(summary.get('pnl') or 0):.2f}"
    ]
    lines.append("by strategy")
    strategies = summary.get("strategies") or {}
    signals = summary.get("signals") or {}
    fills = summary.get("fills") or {}
    names = sorted(set(strategies) | set(signals) | set(fills))
    if not names:
        lines.append("  (none)")
    for name in names:
        row = strategies.get(name) or {"markets": 0, "pnl": 0.0, "win_rate": None, "worst": None}
        label = {"s1": "strategy 1", "s2": "strategy 2"}.get(name, name)
        sig = (signals.get(name) or {}).get("n", signals.get(name, 0)) if isinstance(signals.get(name), dict) else signals.get(name, 0)
        fill = fills.get(name) or {}
        lines.append(
            f"  {label}: signals {sig}  fills {fill.get('n', 0)}  "
            f"bought ${float(fill.get('cost') or 0):.2f}  "
            f"settled {row.get('markets', 0)}  pnl ${float(row.get('pnl') or 0):.2f}  "
            f"win_rate {_pct(row.get('win_rate'))}  worst {_usd(row.get('worst'))}"
        )
    lines.append("by market")
    lanes = summary.get("lanes") or {}
    if not lanes:
        lines.append("  (none)")
    for name, row in lanes.items():
        lines.append(
            f"  {name}: markets {row['markets']}  pnl ${row['pnl']:.2f}  "
            f"win_rate {_pct(row.get('win_rate'))}  worst {_usd(row.get('worst'))}"
        )
    latency = summary.get("latency") or {}
    lines.append("latency ms")
    for key in ("decision_to_post_ms", "recv_to_post_ms"):
        pack = latency.get(key)
        if not pack:
            lines.append(f"  {key}: n/a")
            continue
        lines.append(f"  {key}: n {pack['n']}  p50 {pack['p50']}  p95 {pack['p95']}  max {pack['max']}")
    lines.append("by asset")
    assets = summary.get("assets") or {}
    if not assets:
        lines.append("  (none)")
    for name, row in assets.items():
        lines.append(
            f"  {name}: markets {row['markets']}  pnl ${row['pnl']:.2f}  "
            f"win_rate {_pct(row.get('win_rate'))}  worst {_usd(row.get('worst'))} {row.get('worst_slug') or ''}"
        )
    return "\n".join(lines)


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _usd(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"${float(value):.2f}"
