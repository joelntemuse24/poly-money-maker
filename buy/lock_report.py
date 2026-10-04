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
    """Group settled markets by lane (strategy 1 vs 2) and by asset.

    One settlement row per market. Win rate uses the ``won`` flag.
    Worst market is the lowest ``pnl``.
    """
    by_slug: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict) or row.get("event") != "settlement":
            continue
        slug = str(row.get("slug") or "")
        if not slug:
            continue
        pnl = _num(row.get("pnl"))
        if pnl is None:
            continue
        by_slug[slug] = row

    def bucket() -> dict:
        return {"markets": 0, "wins": 0, "pnl": 0.0, "worst": None, "worst_slug": None}

    lanes: dict[str, dict] = {}
    assets: dict[str, dict] = {}
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

    for row in by_slug.values():
        lane = str(row.get("lane") or "ext")
        asset = str(row.get("asset") or "?")
        duration = str(row.get("duration") or "")
        asset_key = f"{asset}_{duration}" if duration else asset
        add(overall, row)
        add(lanes.setdefault(lane, bucket()), row)
        add(assets.setdefault(asset_key, bucket()), row)

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
    lanes = summary.get("lanes") or {}
    if not lanes:
        lines.append("  (none)")
    for name, row in lanes.items():
        label = "strategy 1 btc 15m" if name == "btc_15m" else "strategy 2 other"
        lines.append(
            f"  {label}: markets {row['markets']}  pnl ${row['pnl']:.2f}  "
            f"win_rate {_pct(row.get('win_rate'))}  worst {_usd(row.get('worst'))}"
        )
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
