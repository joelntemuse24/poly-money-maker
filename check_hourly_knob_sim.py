#!/usr/bin/env python3
"""What-if hourly entry knobs against recorded bot book snapshots.

Live trading is untouched. Reads buybothourly.jsonl-style events from
buybothourly.log (skip/window lines with asks/bids) and optional pathlog
resolution winners.

Hourly pathlog ticks are sparse (~few/hour) so they cannot answer
"would 2s persist have caught that flash?" — the bot log can.

Examples (on the VM):
  .venv/bin/python check_hourly_knob_sim.py --slug bitcoin-up-or-down-september-5-2026-1pm-et
  .venv/bin/python check_hourly_knob_sim.py --compare-persist 8,2,0 --since-hours 24
  .venv/bin/python check_hourly_knob_sim.py --compare-persist 8,2 --day 2026-09-05

Oracle: if pathlog has resolved winner, score hold-to-redeem PnL.
If missing, use research ptb_capture + last live_btc seen on underlying
skips in that window (Binance last vs PTB) as a soft favored-side label
(not a substitute for Gamma resolution when both exist).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent
LOG_DEFAULT = REPO / "buybothourly.log"
RESEARCH_DEFAULT = REPO / "underlying_research_buyhourly.jsonl"
PATHLOG_DEFAULT = REPO / "pathlog" / "ticks"


def _f(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if not math.isfinite(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _parse_ts(ts: Any) -> Optional[float]:
    if isinstance(ts, (int, float)):
        return float(ts)
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # "2026-09-05T17:59:50.605822"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


@dataclass
class Snap:
    ts: float
    slug: str
    cond: str
    up_ask: Optional[float]
    dn_ask: Optional[float]
    up_bid: Optional[float]
    dn_bid: Optional[float]
    up_gui: Optional[float]
    dn_gui: Optional[float]
    live_btc: Optional[float] = None
    ptb: Optional[float] = None
    ptb_source: Optional[str] = None


@dataclass
class MarketTape:
    slug: str
    cond: str
    snaps: List[Snap] = field(default_factory=list)
    winner: Optional[str] = None  # up/down from pathlog
    ptb: Optional[float] = None
    ptb_source: Optional[str] = None
    oracle_side: Optional[str] = None  # from live vs ptb if no winner


def book_ok(
    snap: Snap,
    *,
    ask_min: float,
    ask_max: float,
    min_winner_bid: float,
    max_loser_bid: float,
    max_spread: float,
    min_bid_edge: float,
) -> Tuple[bool, Optional[str], Optional[float], Optional[float], str]:
    """Return (ok, leg, ask, bid, why)."""
    cands: List[Tuple[str, Optional[float], Optional[float]]] = [
        ("up", snap.up_ask, snap.up_bid),
        ("down", snap.dn_ask, snap.dn_bid),
    ]
    best: Optional[Tuple[str, float, float]] = None
    for leg, ask, bid in cands:
        if ask is None:
            continue
        if ask + 1e-12 < ask_min or ask - 1e-12 > ask_max:
            continue
        if bid is None:
            return False, None, None, None, "incomplete_book"
        if bid + 1e-12 < min_winner_bid:
            continue
        spread = ask - bid
        if spread - 1e-12 > max_spread:
            continue
        other_bid = snap.dn_bid if leg == "up" else snap.up_bid
        if other_bid is not None and other_bid - 1e-12 > max_loser_bid:
            return False, None, None, None, "ambiguous"
        if other_bid is not None and (bid - other_bid) + 1e-12 < min_bid_edge:
            return False, None, None, None, "no_consensus"
        if best is None or ask > best[1]:
            best = (leg, ask, bid)
    if best is None:
        # classify rough why
        asks = [a for a in (snap.up_ask, snap.dn_ask) if a is not None]
        if not asks:
            return False, None, None, None, "no_quote"
        if max(asks) < ask_min - 1e-12:
            return False, None, None, None, "ask_below_band"
        return False, None, None, None, "filters"
    return True, best[0], best[1], best[2], "ok"


def first_entry(
    snaps: Sequence[Snap],
    *,
    persist_s: float,
    ask_min: float,
    ask_max: float,
    min_winner_bid: float,
    max_loser_bid: float,
    max_spread: float,
    min_bid_edge: float,
) -> Optional[dict]:
    armed_ts: Optional[float] = None
    armed_leg: Optional[str] = None
    for snap in snaps:
        ok, leg, ask, bid, why = book_ok(
            snap,
            ask_min=ask_min,
            ask_max=ask_max,
            min_winner_bid=min_winner_bid,
            max_loser_bid=max_loser_bid,
            max_spread=max_spread,
            min_bid_edge=min_bid_edge,
        )
        if not ok or leg is None:
            armed_ts = None
            armed_leg = None
            continue
        if persist_s <= 1e-12:
            return {
                "ts": snap.ts,
                "leg": leg,
                "ask": ask,
                "bid": bid,
                "persist_s": 0.0,
                "why": why,
            }
        if armed_ts is None or armed_leg != leg:
            armed_ts = snap.ts
            armed_leg = leg
            continue
        if (snap.ts - armed_ts) + 1e-12 >= persist_s:
            return {
                "ts": snap.ts,
                "leg": leg,
                "ask": ask,
                "bid": bid,
                "persist_s": persist_s,
                "armed_for": snap.ts - armed_ts,
                "why": why,
            }
    return None


def hold_pnl(leg: str, ask: float, budget: float, winner: Optional[str]) -> Optional[float]:
    if winner not in ("up", "down") or ask <= 0:
        return None
    shares = budget / ask
    if leg == winner:
        return shares * 1.0 - budget
    return -budget


def load_winners(pathlog_dir: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not pathlog_dir.is_dir():
        return out
    for path in pathlog_dir.glob("*.jsonl"):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if '"e":"resolved"' not in line and '"e": "resolved"' not in line:
                        continue
                    row = json.loads(line)
                    if row.get("e") != "resolved":
                        continue
                    w = str(row.get("winner") or "").lower()
                    if w in ("up", "down"):
                        # slug from filename stem
                        out[path.stem] = w
        except (OSError, json.JSONDecodeError):
            continue
    return out


def load_ptb(research_path: Path) -> Dict[str, Tuple[float, str]]:
    out: Dict[str, Tuple[float, str]] = {}
    if not research_path.is_file():
        return out
    try:
        with open(research_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") != "ptb_capture":
                    continue
                slug = str(row.get("slug") or "")
                ptb = _f(row.get("ptb"))
                src = str(row.get("source") or "")
                if slug and ptb is not None:
                    out[slug] = (ptb, src)
    except OSError:
        pass
    return out


def load_tapes_from_log(
    log_path: Path,
    *,
    since_ts: Optional[float],
    slug_filter: Optional[str],
) -> Dict[str, MarketTape]:
    tapes: Dict[str, MarketTape] = {}
    # condition_id -> slug from buy_window
    cond_slug: Dict[str, str] = {}
    if not log_path.is_file():
        return tapes
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is None:
                continue
            if since_ts is not None and ts < since_ts:
                continue
            ev = str(row.get("event") or "")
            cond = str(row.get("condition_id") or "")
            slug = str(row.get("slug") or cond_slug.get(cond) or "")
            if ev == "buy_window" and cond and row.get("slug"):
                cond_slug[cond] = str(row["slug"])
                slug = str(row["slug"])
            if slug_filter and slug_filter not in slug and slug_filter not in cond:
                continue
            # Keep book-bearing skip/window lines
            if not any(
                k in row
                for k in ("up_ask", "dn_ask", "up_bid", "dn_bid", "up_gui", "dn_gui")
            ) and ev != "buy_window":
                continue
            if not slug and not cond:
                continue
            key = slug or cond
            if key not in tapes:
                tapes[key] = MarketTape(slug=slug or key, cond=cond)
            elif slug and not tapes[key].slug.startswith("bitcoin"):
                tapes[key].slug = slug
            snap = Snap(
                ts=ts,
                slug=tapes[key].slug,
                cond=cond or tapes[key].cond,
                up_ask=_f(row.get("up_ask")),
                dn_ask=_f(row.get("dn_ask")),
                up_bid=_f(row.get("up_bid")),
                dn_bid=_f(row.get("dn_bid")),
                up_gui=_f(row.get("up_gui")),
                dn_gui=_f(row.get("dn_gui")),
                live_btc=_f(row.get("live_btc")),
                ptb=_f(row.get("ptb")),
                ptb_source=str(row.get("ptb_source") or "") or None,
            )
            # buy_window may lack book — skip empty
            if (
                snap.up_ask is None
                and snap.dn_ask is None
                and snap.up_bid is None
                and snap.dn_bid is None
            ):
                continue
            tapes[key].snaps.append(snap)
    for tape in tapes.values():
        tape.snaps.sort(key=lambda s: s.ts)
    return tapes


def attach_oracle(
    tapes: Dict[str, MarketTape],
    winners: Dict[str, str],
    ptbs: Dict[str, Tuple[float, str]],
) -> None:
    for tape in tapes.values():
        w = winners.get(tape.slug)
        if w:
            tape.winner = w
        ptb_rec = ptbs.get(tape.slug)
        if ptb_rec:
            tape.ptb, tape.ptb_source = ptb_rec
        # soft oracle from last live_btc on tape vs ptb
        live = None
        for s in reversed(tape.snaps):
            if s.live_btc is not None:
                live = s.live_btc
                break
            if s.ptb is not None and tape.ptb is None:
                tape.ptb = s.ptb
                tape.ptb_source = s.ptb_source
        if tape.winner is None and tape.ptb is not None and live is not None:
            if live > tape.ptb:
                tape.oracle_side = "up"
            elif live < tape.ptb:
                tape.oracle_side = "down"


def run_compare(
    tapes: Dict[str, MarketTape],
    persist_list: Sequence[float],
    *,
    ask_min: float,
    ask_max: float,
    min_winner_bid: float,
    max_loser_bid: float,
    max_spread: float,
    min_bid_edge: float,
    budget: float,
) -> None:
    print(
        "slug\t"
        + "\t".join(f"p{p:g}_leg/ask/pnl" for p in persist_list)
        + "\twinner\toracle\tptb\tsnaps"
    )
    totals = {
        p: {"n": 0, "fills": 0, "pnl": 0.0, "known": 0} for p in persist_list
    }
    for slug in sorted(tapes.keys()):
        tape = tapes[slug]
        if not tape.snaps:
            continue
        cells = []
        for p in persist_list:
            hit = first_entry(
                tape.snaps,
                persist_s=p,
                ask_min=ask_min,
                ask_max=ask_max,
                min_winner_bid=min_winner_bid,
                max_loser_bid=max_loser_bid,
                max_spread=max_spread,
                min_bid_edge=min_bid_edge,
            )
            totals[p]["n"] += 1
            if not hit:
                cells.append("MISS")
                continue
            totals[p]["fills"] += 1
            side = tape.winner or tape.oracle_side
            pnl = hold_pnl(hit["leg"], float(hit["ask"]), budget, side)
            if pnl is not None:
                totals[p]["pnl"] += pnl
                totals[p]["known"] += 1
                cells.append(
                    f"{hit['leg']}@{hit['ask']:.2f}/{pnl:+.2f}"
                )
            else:
                cells.append(f"{hit['leg']}@{hit['ask']:.2f}/?")
        print(
            f"{tape.slug}\t"
            + "\t".join(cells)
            + f"\t{tape.winner or '-'}\t{tape.oracle_side or '-'}\t"
            f"{tape.ptb if tape.ptb is not None else '-'}\t{len(tape.snaps)}"
        )
    print()
    print("aggregate (hold-to-redeem; oracle used only when winner missing):")
    for p in persist_list:
        t = totals[p]
        print(
            f"  persist={p:g}s  markets={t['n']}  fills={t['fills']}  "
            f"scored={t['known']}  pnl_sum=${t['pnl']:+.2f}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", type=Path, default=LOG_DEFAULT)
    ap.add_argument("--research", type=Path, default=RESEARCH_DEFAULT)
    ap.add_argument("--pathlog-dir", type=Path, default=PATHLOG_DEFAULT)
    ap.add_argument("--slug", type=str, default=None, help="substring filter")
    ap.add_argument("--since-hours", type=float, default=None)
    ap.add_argument("--day", type=str, default=None, help="UTC day YYYY-MM-DD")
    ap.add_argument(
        "--compare-persist",
        type=str,
        default="8,2,0",
        help="comma list of persist seconds to compare",
    )
    ap.add_argument("--ask-min", type=float, default=0.949)
    ap.add_argument("--ask-max", type=float, default=0.99)
    ap.add_argument("--min-winner-bid", type=float, default=0.90)
    ap.add_argument("--max-loser-bid", type=float, default=0.10)
    ap.add_argument("--max-spread", type=float, default=0.05)
    ap.add_argument("--min-bid-edge", type=float, default=0.05)
    ap.add_argument("--budget", type=float, default=5.0)
    args = ap.parse_args(argv)

    since_ts = None
    if args.since_hours is not None:
        since_ts = datetime.now(timezone.utc).timestamp() - args.since_hours * 3600
    if args.day:
        day0 = datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        since_ts = day0.timestamp()
        # still load only that day: filter end in attach by cutting snaps later
        day_end = day0 + timedelta(days=1)

    persist_list = [float(x) for x in args.compare_persist.split(",") if x.strip()]
    tapes = load_tapes_from_log(
        args.log, since_ts=since_ts, slug_filter=args.slug
    )
    if args.day:
        day_end_ts = (day0 + timedelta(days=1)).timestamp()
        for tape in tapes.values():
            tape.snaps = [s for s in tape.snaps if s.ts < day_end_ts]
    winners = load_winners(args.pathlog_dir)
    ptbs = load_ptb(args.research)
    attach_oracle(tapes, winners, ptbs)
    # drop empty
    tapes = {k: v for k, v in tapes.items() if v.snaps}
    if not tapes:
        print("no book snapshots found in log for that filter", file=sys.stderr)
        return 1
    print(
        f"# tapes={len(tapes)} ask=[{args.ask_min},{args.ask_max}] "
        f"bid>={args.min_winner_bid} spread<={args.max_spread} "
        f"budget=${args.budget:g} persist={persist_list}"
    )
    run_compare(
        tapes,
        persist_list,
        ask_min=args.ask_min,
        ask_max=args.ask_max,
        min_winner_bid=args.min_winner_bid,
        max_loser_bid=args.max_loser_bid,
        max_spread=args.max_spread,
        min_bid_edge=args.min_bid_edge,
        budget=args.budget,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
