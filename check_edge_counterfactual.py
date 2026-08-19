#!/usr/bin/env python3
"""Counterfactual: resolution win rate if underlying-edge skips had been buys.

Reads buy_skip_underlying_edge / decision_skip_underlying from research JSONL
and bot logs, infers the GUI-winner leg we would have bought, then resolves
those markets via Gamma (slug / question search).

This is resolution accuracy only — it cannot reconstruct false hedges (no
historical book). Safe / read-only: does not place orders or edit state.

Usage (on the VM, from the repo root):
  python check_edge_counterfactual.py
  python check_edge_counterfactual.py --bot 5m
  python check_edge_counterfactual.py --near-miss-usd 2
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

BOTS = {
    "5m": {
        "research": "underlying_research_buy5m.jsonl",
        "log": "buybot5m.log",
        "min_edge": 0.0,
    },
    "15m": {
        "research": "underlying_research_buy.jsonl",
        "log": "buybot.log",
        "min_edge": 10.0,
    },
    "hr": {
        "research": "underlying_research_buyhourly.jsonl",
        "log": "buybothourly.log",
        "min_edge": 10.0,
    },
}

SKIP_EVENTS = frozenset(
    {"decision_skip_underlying", "buy_skip_underlying_edge"}
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "poly-money-maker-edge-counterfactual"
    return s


def _parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def load_skips(research: str, log: str) -> Dict[str, dict]:
    """Unique condition_id -> best skip row (prefer research with slug/question)."""
    by: Dict[str, dict] = {}
    for path in (research, log):
        try:
            handle = open(path, encoding="utf-8")
        except FileNotFoundError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if event.get("event") not in SKIP_EVENTS:
                    continue
                cid = event.get("condition_id")
                if not cid:
                    continue
                prev = by.get(cid)
                if prev is None:
                    by[cid] = event
                    continue
                # Prefer research rows; otherwise prefer rows that carry slug/question.
                if (
                    event.get("event") == "decision_skip_underlying"
                    and prev.get("event") != "decision_skip_underlying"
                ):
                    by[cid] = event
                elif not prev.get("slug") and event.get("slug"):
                    by[cid] = event
                elif not prev.get("question") and event.get("question"):
                    by[cid] = event
    return by


def intended_leg(event: dict) -> Optional[str]:
    up, dn = event.get("up_gui"), event.get("dn_gui")
    if up is None or dn is None:
        return None
    try:
        up_f, dn_f = float(up), float(dn)
    except (TypeError, ValueError):
        return None
    if up_f > dn_f:
        return "up"
    if dn_f > up_f:
        return "down"
    return None


def winner_from_market(market: Optional[dict]) -> Tuple[Optional[str], str]:
    if not market:
        return None, "missing"
    prices = _parse_json_field(market.get("outcomePrices"))
    outcomes = _parse_json_field(market.get("outcomes"))
    closed = bool(market.get("closed"))
    if not prices or not outcomes or len(prices) != len(outcomes):
        return None, "no_prices"
    try:
        pairs = [(str(o).lower(), float(p)) for o, p in zip(outcomes, prices)]
    except (TypeError, ValueError):
        return None, "bad_prices"
    best_name, best_px = max(pairs, key=lambda item: item[1])
    if best_px < 0.99:
        return None, f"unresolved_px={best_px} closed={closed}"
    if "up" in best_name:
        return "up", "ok"
    if "down" in best_name:
        return "down", "ok"
    return None, f"unknown={best_name}"


def resolve_market(session: requests.Session, event: dict) -> Optional[dict]:
    """Gamma does not reliably filter by condition_ids — use slug / search."""
    slug = event.get("slug")
    question = event.get("question")
    cid = str(event.get("condition_id") or "")

    if slug:
        r = session.get(
            f"https://gamma-api.polymarket.com/markets/slug/{slug}",
            timeout=15,
        )
        if r.status_code == 200 and r.json():
            return r.json()
        r = session.get(
            f"https://gamma-api.polymarket.com/events/slug/{slug}",
            timeout=15,
        )
        if r.status_code == 200 and r.json():
            markets = r.json().get("markets") or []
            if markets:
                return markets[0]

    if question:
        r = session.get(
            "https://gamma-api.polymarket.com/public-search",
            params={"q": question},
            timeout=15,
        )
        if r.status_code == 200:
            for ev in (r.json() or {}).get("events") or []:
                for market in ev.get("markets") or []:
                    if cid and str(market.get("conditionId")) == cid:
                        return market
                    if market.get("question") == question:
                        return market
                if ev.get("title") == question and (ev.get("markets") or []):
                    return ev["markets"][0]
    return None


def _edge(event: dict) -> Optional[float]:
    raw = event.get("edge_usd")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def analyze_bot(
    session: requests.Session,
    label: str,
    cfg: dict,
    near_miss_usd: float,
    sleep_s: float,
    debug_unresolved: int,
) -> None:
    skips = load_skips(cfg["research"], cfg["log"])
    min_edge = float(cfg["min_edge"])
    print(f"\n===== {label} edge-skips unique markets: {len(skips)} (min=${min_edge:g}) =====")
    if not skips:
        print(f"  (no rows in {cfg['research']} / {cfg['log']})")
        return

    resolved: List[dict] = []
    openish = 0
    debug_left = debug_unresolved

    for cid, event in skips.items():
        leg = intended_leg(event)
        market = resolve_market(session, event)
        if sleep_s > 0:
            time.sleep(sleep_s)
        winner, why = winner_from_market(market)
        if winner is None or leg is None:
            openish += 1
            if debug_left > 0:
                debug_left -= 1
                print(
                    "  DEBUG unresolved:",
                    event.get("slug") or cid[:18],
                    event.get("question"),
                    "leg=",
                    leg,
                    "why=",
                    why,
                    "market=",
                    None
                    if not market
                    else {
                        "closed": market.get("closed"),
                        "prices": market.get("outcomePrices"),
                        "outcomes": market.get("outcomes"),
                    },
                )
            continue
        edge = _edge(event)
        resolved.append(
            {
                "won": leg == winner,
                "leg": leg,
                "winner": winner,
                "edge": edge,
                "up_gui": event.get("up_gui"),
                "dn_gui": event.get("dn_gui"),
                "q": (event.get("question") or event.get("slug") or cid)[:70],
            }
        )

    print(f"resolved: {len(resolved)}  open/unknown: {openish}")
    if not resolved:
        return

    wins = sum(1 for row in resolved if row["won"])
    print(
        f"COUNTERFACTUAL win rate (GUI winner): "
        f"{wins}/{len(resolved)} = {wins / len(resolved):.1%}"
    )

    def bucket(name: str, rows: Iterable[dict]) -> None:
        rows = list(rows)
        if not rows:
            return
        w = sum(1 for row in rows if row["won"])
        print(f"  {name}: {w}/{len(rows)} = {w / len(rows):.1%}")

    bucket(
        f"|edge| < ${min_edge:g}",
        (r for r in resolved if r["edge"] is not None and abs(r["edge"]) < min_edge),
    )
    bucket(
        f"near-miss ${min_edge - near_miss_usd:g}–{min_edge:g}",
        (
            r
            for r in resolved
            if r["edge"] is not None
            and (min_edge - near_miss_usd) <= abs(r["edge"]) < min_edge
        ),
    )
    bucket("edge=None (PTB/live missing)", (r for r in resolved if r["edge"] is None))

    losses = [r for r in resolved if not r["won"]]
    print(f"losses: {len(losses)}")
    for row in losses[:20]:
        print(
            f"  edge={row['edge']} would={row['leg']} won={row['winner']} "
            f"gui={row['up_gui']}/{row['dn_gui']} {row['q']}"
        )
    if len(losses) > 20:
        print(f"  ... {len(losses) - 20} more")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolution counterfactual for underlying-edge skips",
    )
    parser.add_argument(
        "--bot",
        choices=sorted(BOTS),
        help="Only analyze one bot (default: all)",
    )
    parser.add_argument(
        "--near-miss-usd",
        type=float,
        default=2.0,
        help="Width of near-miss bucket under min_edge (default 2)",
    )
    parser.add_argument(
        "--sleep-s",
        type=float,
        default=0.08,
        help="Delay between Gamma lookups",
    )
    parser.add_argument(
        "--debug-unresolved",
        type=int,
        default=3,
        help="Print this many unresolved lookups per bot for debugging",
    )
    args = parser.parse_args()

    session = _session()
    selected = (
        {args.bot: BOTS[args.bot]}
        if args.bot
        else BOTS
    )
    for label, cfg in selected.items():
        analyze_bot(
            session,
            label,
            cfg,
            near_miss_usd=float(args.near_miss_usd),
            sleep_s=float(args.sleep_s),
            debug_unresolved=int(args.debug_unresolved),
        )
    print(
        "\nResolution-only (ignores false hedges). "
        "Compare to live redeem rate on actual entries."
    )


if __name__ == "__main__":
    main()
