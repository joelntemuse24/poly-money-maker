#!/usr/bin/env python3
"""Join live 5m fills to ~1s pathlog books and score persist 0/1/2/5s.

Read-only. No orders. The 27–31 Aug join failed when a VM snippet required
``slug`` on ``buy_fill``: journal fills carry ``token_id`` (and
``buy_success`` carries ``condition_id``). Research JSONL has ``slug`` /
``start_ts`` and ``logged_at`` (unix), not ``ts``.

Usage (VM, copy the file off this branch — do not checkout over live).
``cd`` into the repo first so ``buy/`` is importable, or pass ``--repo``
(the script puts that path on ``sys.path`` even when the file lives in
``/tmp``):

  cd ~/poly-money-maker
  git fetch origin cursor/last120-loss-catalog-f488
  git show origin/cursor/last120-loss-catalog-f488:check_last120_tick_autopsy.py \\
      > /tmp/check_last120_tick_autopsy.py
  python3 /tmp/check_last120_tick_autopsy.py --repo "$PWD" \\
      --out /tmp/last120-research --since 2026-08-27T17:26:00

Paste the printed report (also written to ``$OUT/autopsy.txt``).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def repo_from_argv(argv: Optional[Sequence[str]] = None) -> Path:
    """``--repo`` if passed, else cwd. Not ``__file__`` (copies live in /tmp)."""
    args = list(sys.argv if argv is None else argv)
    for i, arg in enumerate(args):
        if arg == "--repo" and i + 1 < len(args):
            return Path(args[i + 1]).expanduser().resolve()
    return Path.cwd().resolve()


def bootstrap_sys_path(argv: Optional[Sequence[str]] = None) -> Path:
    repo = repo_from_argv(argv)
    path = str(repo)
    if path not in sys.path:
        sys.path.insert(0, path)
    return repo


REPO = bootstrap_sys_path()

from buy.hedge_gate import evaluate_held_bag, hedge_qualify_ok
from buy.live_journal import default_journal_path, iter_rotated_paths
from check_path_backtest import (
    _f,
    _leg_quote,
    first_entry,
    infer_winner,
    load_market_file,
    matches_series,
    simulate_fak_buy,
)
OVERLAY_SINCE_ISO = "2026-08-27T17:26:00"
OVERLAY_START_UNIX = 1787851560
PATHLOG_GUI_SPREAD = 0.10
LIVE_DUMP = 0.32
LIVE_QUALIFY = 0.50
LIVE_ASK_MAX = 0.52
LIVE_RECOVERY = 0.53
LIVE_SPREAD = 0.15
LIVE_MIN_EDGE = 0.05
PERSIST_GRID = (0.0, 1.0, 2.0, 5.0)
BUDGET = 2.5

# Recap-named last-120 losers the operator copied on 31 Aug. 25 of these
# were already missing on the VM (prune). The rest are still worth walking
# even when the journal join is empty.
NAMED_LOSS_SLUGS = (
    "btc-updown-5m-1787898600",
    "btc-updown-5m-1787915700",
    "btc-updown-5m-1787918400",
    "btc-updown-5m-1787925900",
    "btc-updown-5m-1787926800",
    "btc-updown-5m-1787927100",
    "btc-updown-5m-1787936100",
    "btc-updown-5m-1787938200",
    "btc-updown-5m-1787940000",
    "btc-updown-5m-1787940900",
    "btc-updown-5m-1787945100",
    "btc-updown-5m-1787945700",
    "btc-updown-5m-1787957700",
    "btc-updown-5m-1787960100",
    "btc-updown-5m-1787962800",
    "btc-updown-5m-1787964000",
    "btc-updown-5m-1787964600",
    "btc-updown-5m-1787965800",
    "btc-updown-5m-1787969100",
    "btc-updown-5m-1787973000",
    "btc-updown-5m-1787977500",
    "btc-updown-5m-1787979000",
    "btc-updown-5m-1787982300",
    "btc-updown-5m-1787984700",
    "btc-updown-5m-1787993400",
    "btc-updown-5m-1788000600",
    "btc-updown-5m-1788002700",
    "btc-updown-5m-1788005100",
    "btc-updown-5m-1788013500",
    "btc-updown-5m-1788025800",
    "btc-updown-5m-1788033900",
    "btc-updown-5m-1788042600",
    "btc-updown-5m-1788046200",
    "btc-updown-5m-1788050700",
    "btc-updown-5m-1788056700",
    "btc-updown-5m-1788060000",
    "btc-updown-5m-1788069600",
    "btc-updown-5m-1788078300",
    "btc-updown-5m-1788084900",
    "btc-updown-5m-1788094800",
    "btc-updown-5m-1788100200",
    "btc-updown-5m-1788104100",
    "btc-updown-5m-1788104400",
    "btc-updown-5m-1788104700",
    "btc-updown-5m-1788105000",
    "btc-updown-5m-1788116100",
    "btc-updown-5m-1788119100",
    "btc-updown-5m-1788122100",
    "btc-updown-5m-1788143400",
    "btc-updown-5m-1788144900",
    "btc-updown-5m-1788145500",
    "btc-updown-5m-1788146700",
    "btc-updown-5m-1788155700",
    "btc-updown-5m-1788172800",
    "btc-updown-5m-1788174300",
    "btc-updown-5m-1788183600",
)

FILL_EVENTS = frozenset(
    {"buy_fill", "buy_fill_below_band", "buy_ghost_fill", "buy_success"}
)
RESEARCH_FILL_EVENTS = frozenset({"buy_fill", "buy_ghost_fill"})


def parse_iso_unix(raw: str, *, assume_utc: bool = True) -> float:
    text = str(raw or "").strip().replace("Z", "+00:00")
    if not text:
        raise ValueError("empty timestamp")
    clock = datetime.fromisoformat(text)
    if clock.tzinfo is None:
        if assume_utc:
            clock = clock.replace(tzinfo=timezone.utc)
        else:
            clock = clock.astimezone()
    return clock.timestamp()


def event_unix(row: dict) -> Optional[float]:
    ts = row.get("ts")
    if ts not in (None, ""):
        try:
            if isinstance(ts, (int, float)):
                return float(ts)
            return parse_iso_unix(str(ts))
        except (TypeError, ValueError):
            pass
    logged = row.get("logged_at")
    if logged not in (None, ""):
        try:
            return float(logged)
        except (TypeError, ValueError):
            return None
    return None


def extract_json_obj(line: str) -> Optional[dict]:
    text = (line or "").strip()
    if not text:
        return None
    if not text.startswith("{"):
        brace = text.find("{")
        if brace < 0:
            return None
        text = text[brace:]
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def rotated_paths(primary: Path, backups: int = 16) -> List[Path]:
    paths: List[Path] = []
    for n in range(int(backups), 0, -1):
        rotated = Path(f"{primary}.{n}")
        if rotated.is_file():
            paths.append(rotated)
    if primary.is_file():
        paths.append(primary)
    return paths


def iter_jsonl(paths: Iterable[Path]) -> Iterable[Tuple[Path, dict]]:
    for path in paths:
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                row = extract_json_obj(line)
                if row is not None:
                    yield path, row


def slug_from_start_ts(start_ts: Any) -> Optional[str]:
    try:
        stamp = int(float(start_ts))
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return f"btc-updown-5m-{stamp}"


def index_tick_headers(tick_dir: Path) -> Dict[str, Any]:
    """Map token_id / condition_id / start_ts → slug from pathlog open rows."""
    by_token: Dict[str, str] = {}
    by_cid: Dict[str, str] = {}
    by_start: Dict[int, str] = {}
    headers: Dict[str, dict] = {}
    files = 0
    for path in sorted(tick_dir.glob("btc-updown-5m-*.jsonl")):
        files += 1
        try:
            with path.open(encoding="utf-8") as handle:
                first = handle.readline()
        except OSError:
            continue
        header = extract_json_obj(first)
        if not header or header.get("e") != "open":
            header = {"slug": path.stem}
        slug = str(header.get("slug") or path.stem)
        headers[slug] = header
        cid = str(header.get("cid") or "").strip()
        if cid:
            by_cid[cid] = slug
        for key in ("up", "dn"):
            token = str(header.get(key) or "").strip()
            if token:
                by_token[token] = slug
        try:
            start = int(float(header.get("start") or path.stem.rsplit("-", 1)[-1]))
        except (TypeError, ValueError):
            start = 0
        if start:
            by_start[start] = slug
    return {
        "files": files,
        "by_token": by_token,
        "by_cid": by_cid,
        "by_start": by_start,
        "headers": headers,
    }


def resolve_slug(row: dict, index: Dict[str, Any]) -> Optional[str]:
    slug = str(row.get("slug") or "").strip()
    if slug:
        return slug
    token = str(row.get("token_id") or "").strip()
    if token and token in index["by_token"]:
        return index["by_token"][token]
    cid = str(row.get("condition_id") or "").strip()
    if cid and cid in index["by_cid"]:
        return index["by_cid"][cid]
    start = row.get("start_ts") or row.get("start")
    built = slug_from_start_ts(start)
    if built:
        return built
    return None


def resolve_leg(row: dict, index: Dict[str, Any], slug: Optional[str]) -> Optional[str]:
    leg = str(row.get("leg") or "").strip().lower()
    if leg in ("up", "down"):
        return leg
    token = str(row.get("token_id") or "").strip()
    header = index["headers"].get(slug or "")
    if not token or not header:
        return None
    if token == str(header.get("up") or ""):
        return "up"
    if token == str(header.get("dn") or ""):
        return "down"
    return None


def fill_avg(row: dict) -> Optional[float]:
    for key in ("avg_price", "price", "ask_gate", "ask"):
        value = _f(row.get(key))
        if value is not None and 0 < value <= 1:
            return value
    return None


def collect_source_stats(paths: Sequence[Path], *, since_unix: float) -> dict:
    counts: Counter = Counter()
    sample_keys: Dict[str, List[str]] = {}
    n_json = 0
    n_after = 0
    first_ts = last_ts = None
    for _path, row in iter_jsonl(paths):
        n_json += 1
        name = str(row.get("event") or row.get("e") or "")
        stamp = event_unix(row)
        if stamp is not None:
            if first_ts is None or stamp < first_ts:
                first_ts = stamp
            if last_ts is None or stamp > last_ts:
                last_ts = stamp
            if stamp + 1e-9 < since_unix:
                continue
        n_after += 1
        if name:
            counts[name] += 1
        if name in FILL_EVENTS | RESEARCH_FILL_EVENTS and name not in sample_keys:
            sample_keys[name] = sorted(row.keys())
    bytes_total = sum(p.stat().st_size for p in paths if p.is_file())
    return {
        "paths": [p.name for p in paths],
        "n_files": len(paths),
        "bytes": bytes_total,
        "n_json": n_json,
        "n_after_since": n_after,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "counts": counts,
        "sample_keys": sample_keys,
    }


def collect_fills(
    rows: Iterable[dict],
    index: Dict[str, Any],
    *,
    since_unix: float,
) -> List[dict]:
    """Build one fill per slug (earliest). Does not require ``slug`` on the row."""
    by_slug: Dict[str, dict] = {}
    for row in rows:
        name = str(row.get("event") or "")
        if name not in FILL_EVENTS and name not in RESEARCH_FILL_EVENTS:
            continue
        stamp = event_unix(row)
        if stamp is not None and stamp + 1e-9 < since_unix:
            continue
        slug = resolve_slug(row, index)
        if not slug:
            continue
        avg = fill_avg(row)
        leg = resolve_leg(row, index, slug)
        current = by_slug.get(slug)
        if current is not None and stamp is not None and current["ts"] is not None:
            if stamp >= current["ts"]:
                continue
        by_slug[slug] = {
            "slug": slug,
            "ts": stamp,
            "leg": leg,
            "avg": avg,
            "event": name,
            "source_keys": sorted(row.keys()),
        }
    return sorted(by_slug.values(), key=lambda item: (item["ts"] or 0, item["slug"]))


def pathlog_gui_ok(
    held_bid,
    held_ask,
    other_bid,
    other_ask,
    *,
    held_gui_max: float = LIVE_ASK_MAX,
    other_gui_min: float = 0.48,
    min_edge: float = LIVE_MIN_EDGE,
    last_trade_max: float = LIVE_ASK_MAX,
) -> Tuple[bool, str]:
    """Tight-mid GUI proxy. Pathlog has no last-trade print."""
    if held_bid is None or held_ask is None or other_bid is None or other_ask is None:
        return False, "incomplete_gui"
    if held_ask < held_bid or other_ask < other_bid:
        return False, "crossed"
    if (held_ask - held_bid) > PATHLOG_GUI_SPREAD + 1e-12:
        return False, "wide_held"
    if (other_ask - other_bid) > PATHLOG_GUI_SPREAD + 1e-12:
        return False, "wide_other"
    held_gui = (held_bid + held_ask) / 2.0
    other_gui = (other_bid + other_ask) / 2.0
    if held_gui > last_trade_max + 1e-12:
        return False, "last_trade_too_high"
    if held_gui > held_gui_max + 1e-12:
        return False, "held_gui_high"
    if other_gui + 1e-12 < other_gui_min:
        return False, "other_gui_low"
    if abs(held_gui - other_gui) + 1e-12 < min_edge:
        return False, "ambiguous"
    return True, "ok"


def clamp_fill_ts(fill_ts: Optional[float], start_ts: float, end_ts: float) -> Optional[float]:
    if fill_ts is None:
        return None
    if start_ts - 5 <= fill_ts <= end_ts + 5:
        return fill_ts
    dublin = fill_ts - 3600.0
    if start_ts - 5 <= dublin <= end_ts + 5:
        return dublin
    return fill_ts


def book_stats_after(ticks: Sequence[dict], held: str, after_ts: float) -> dict:
    other = "down" if held == "up" else "up"
    min_bid = None
    first50 = first32 = None
    max_book_run = 0.0
    max_gui_run = 0.0
    book_start = gui_start = None
    n_after = 0
    dts: List[float] = []
    prev_ts = None
    for row in ticks:
        ts = _f(row.get("ts"))
        if ts is None or ts <= after_ts + 1e-12:
            continue
        n_after += 1
        if prev_ts is not None:
            dts.append(ts - prev_ts)
        prev_ts = ts
        bid, ask, _ = _leg_quote(row, held)
        other_bid, other_ask, _ = _leg_quote(row, other)
        if bid is None:
            book_start = gui_start = None
            continue
        min_bid = bid if min_bid is None else min(min_bid, bid)
        if first50 is None and bid <= LIVE_QUALIFY + 1e-12:
            first50 = ts
        if first32 is None and bid <= LIVE_DUMP + 1e-12:
            first32 = ts
        book_ok, _why = hedge_qualify_ok(
            bid, ask, LIVE_QUALIFY, LIVE_SPREAD, LIVE_ASK_MAX,
        )
        if book_ok:
            if book_start is None:
                book_start = ts
            max_book_run = max(max_book_run, ts - book_start)
        else:
            book_start = None
        gui_ok, _gwhy = pathlog_gui_ok(bid, ask, other_bid, other_ask)
        if book_ok and gui_ok:
            if gui_start is None:
                gui_start = ts
            max_gui_run = max(max_gui_run, ts - gui_start)
        else:
            gui_start = None
    median_dt = statistics.median(dts) if dts else None
    return {
        "n_after": n_after,
        "min_bid": min_bid,
        "first50": first50,
        "first32": first32,
        "max_book_run_s": max_book_run,
        "max_gui_run_s": max_gui_run,
        "median_dt_s": median_dt,
    }


def walk_live_exit(
    ticks: Sequence[dict],
    held: str,
    *,
    fill_ts: float,
    persist_s: float,
    require_gui: bool,
    avg: float,
    winner: Optional[str],
    budget: float = BUDGET,
) -> dict:
    """Replay live 5m dump/persist/fade/recovery on pathlog ticks. No oracle."""
    if held not in ("up", "down") or avg is None or avg <= 0:
        return {"exit": "no_fill", "exit_bid": None, "pnl": None, "won": None}
    shares = budget / float(avg)
    notional = float(budget)
    other = "down" if held == "up" else "up"
    armed = None
    persist_done = False
    for row in ticks:
        ts = _f(row.get("ts"))
        if ts is None or ts <= fill_ts + 1e-12:
            continue
        bid, ask, _ = _leg_quote(row, held)
        other_bid, other_ask, _ = _leg_quote(row, other)
        if require_gui:
            gui_ok, gui_why = pathlog_gui_ok(bid, ask, other_bid, other_ask)
        else:
            gui_ok, gui_why = True, "book_only"
        intent = evaluate_held_bag(
            bid,
            ask,
            now_s=ts,
            persist_armed_ts=armed,
            persist_s=persist_s,
            dump_bid_max=LIVE_DUMP,
            qualify_bid=LIVE_QUALIFY,
            qualify_ask_max=LIVE_ASK_MAX,
            max_spread=LIVE_SPREAD,
            persist_done=persist_done,
            gui_ok=gui_ok,
            gui_why=gui_why,
            recovery_cancel=LIVE_RECOVERY,
            sell_fade=True,
        )
        persist_done = bool(intent.persist_done)
        armed = intent.persist_ts
        if intent.action not in {"sell", "dump"}:
            continue
        px = float(intent.sell_at)
        proceeds = shares * px
        won = False
        pnl = round(proceeds - notional, 4)
        label = "dump" if intent.dump else "persist"
        return {
            "exit": label,
            "exit_reason": intent.reason,
            "exit_bid": px,
            "pnl": pnl,
            "won": won,
            "winner_dump": bool(winner == held),
        }
    if winner is None:
        return {
            "exit": "unresolved",
            "exit_reason": "unresolved",
            "exit_bid": None,
            "pnl": None,
            "won": None,
            "winner_dump": False,
        }
    won = held == winner
    return {
        "exit": "redeem_win" if won else "redeem_loss",
        "exit_reason": "redeem",
        "exit_bid": None,
        "pnl": round((shares - notional) if won else -notional, 4),
        "won": won,
        "winner_dump": False,
    }


def tick_density(tick_dir: Path, slugs: Optional[Sequence[str]] = None) -> dict:
    counts: List[int] = []
    bytes_list: List[int] = []
    empty = 0
    matched = 0
    slug_set = set(slugs) if slugs is not None else None
    for path in sorted(tick_dir.glob("btc-updown-5m-*.jsonl")):
        if slug_set is not None and path.stem not in slug_set:
            continue
        matched += 1
        n_ticks = 0
        try:
            size = path.stat().st_size
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if '"e":"tick"' in line or '"e": "tick"' in line:
                        n_ticks += 1
        except OSError:
            continue
        bytes_list.append(size)
        counts.append(n_ticks)
        if n_ticks == 0:
            empty += 1
    if not counts:
        return {
            "n_files": matched,
            "empty": empty,
            "mean_ticks": None,
            "p50_ticks": None,
            "p90_ticks": None,
            "mean_bytes": None,
            "total_bytes": 0,
        }
    ordered = sorted(counts)
    p90_i = min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))
    return {
        "n_files": matched,
        "empty": empty,
        "mean_ticks": round(sum(counts) / len(counts), 2),
        "p50_ticks": statistics.median(counts),
        "p90_ticks": ordered[p90_i],
        "mean_bytes": round(sum(bytes_list) / len(bytes_list), 1),
        "total_bytes": sum(bytes_list),
    }


def overlay_slugs(tick_dir: Path, since_unix: float, until_unix: Optional[float] = None) -> List[str]:
    out: List[str] = []
    for path in sorted(tick_dir.glob("btc-updown-5m-*.jsonl")):
        try:
            start = int(path.stem.rsplit("-", 1)[-1])
        except ValueError:
            continue
        if start < since_unix:
            continue
        if until_unix is not None and start > until_unix:
            continue
        out.append(path.stem)
    return out


def synthetic_hit(ticks: Sequence[dict], winner: Optional[str]) -> Optional[dict]:
    hit = first_entry(
        ticks, ask_min=0.75, ask_max=0.90, ttm_min=0.0, ttm_max=120.0,
    )
    if hit is not None:
        return hit
    if winner in ("up", "down"):
        held = "down" if winner == "up" else "up"
        for row in ticks:
            ttm = _f(row.get("ttm"))
            if ttm is None or ttm > 120.0:
                continue
            bid, ask, _ = _leg_quote(row, held)
            if ask is None:
                continue
            return {
                "ts": row.get("ts"),
                "ttm": ttm,
                "leg": held,
                "ask": ask,
                "bid": bid,
                "ask_size": None,
            }
    return None


def persist_grid_rows(
    ticks: Sequence[dict],
    held: str,
    fill_ts: float,
    avg: float,
    winner: Optional[str],
) -> List[dict]:
    rows = []
    for persist_s in PERSIST_GRID:
        for require_gui in (True, False):
            settled = walk_live_exit(
                ticks,
                held,
                fill_ts=fill_ts,
                persist_s=persist_s,
                require_gui=require_gui,
                avg=avg,
                winner=winner,
            )
            rows.append(
                {
                    "persist_s": persist_s,
                    "gui": require_gui,
                    **settled,
                }
            )
    return rows


def summarize_grid(rows: Sequence[dict], *, losers_only: bool = False) -> List[str]:
    grouped: Dict[Tuple[float, bool], List[dict]] = defaultdict(list)
    for row in rows:
        if losers_only:
            won = row.get("won")
            winner_dump = row.get("winner_dump")
            if won is True:
                continue
            if row.get("exit") in {"redeem_win"}:
                continue
            if winner_dump:
                continue
            if won is None and row.get("exit") not in {"dump", "persist", "redeem_loss"}:
                continue
        grouped[(row["persist_s"], row["gui"])].append(row)
    lines = [
        "persist_s  gui   n  dump  persist  redeem_loss  redeem_win  unresolved  "
        "pnl_all  pnl_exit  winner_dumps  med_exit_bid"
    ]
    for persist_s in PERSIST_GRID:
        for gui in (True, False):
            chunk = grouped.get((persist_s, gui), [])
            if not chunk:
                lines.append(
                    f"{persist_s:<9g} {str(gui):<5} 0"
                )
                continue
            exits = Counter(str(r.get("exit")) for r in chunk)
            pnls = [r["pnl"] for r in chunk if r.get("pnl") is not None]
            exit_pnls = [
                r["pnl"]
                for r in chunk
                if r.get("exit") in {"dump", "persist"} and r.get("pnl") is not None
            ]
            bids = [
                r["exit_bid"]
                for r in chunk
                if r.get("exit") in {"dump", "persist"} and r.get("exit_bid") is not None
            ]
            dumps = sum(1 for r in chunk if r.get("winner_dump"))
            med = statistics.median(bids) if bids else None
            lines.append(
                f"{persist_s:<9g} {str(gui):<5} {len(chunk):<3} "
                f"{exits.get('dump', 0):<5} {exits.get('persist', 0):<8} "
                f"{exits.get('redeem_loss', 0):<12} {exits.get('redeem_win', 0):<11} "
                f"{exits.get('unresolved', 0):<11} "
                f"{sum(pnls) if pnls else 0:7.2f} "
                f"{sum(exit_pnls) if exit_pnls else 0:8.2f} "
                f"{dumps:<13} "
                f"{'' if med is None else f'{med:.3f}'}"
            )
    return lines


def fmt_unix(stamp: Optional[float]) -> str:
    if stamp is None:
        return "-"
    return datetime.fromtimestamp(float(stamp), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def print_source_diag(label: str, stats: dict, lines: List[str]) -> None:
    lines.append(f"== {label} ==")
    lines.append(
        f"files={stats['n_files']} bytes={stats['bytes']} json={stats['n_json']} "
        f"after_since={stats['n_after_since']}"
    )
    lines.append(f"names: {', '.join(stats['paths'][:8]) or '(none)'}")
    lines.append(f"first={fmt_unix(stats['first_ts'])} last={fmt_unix(stats['last_ts'])}")
    if stats["counts"]:
        top = stats["counts"].most_common(12)
        lines.append("counts: " + " ".join(f"{n}:{c}" for n, c in top))
    if stats["sample_keys"]:
        for name, keys in stats["sample_keys"].items():
            lines.append(f"sample_keys {name}: {', '.join(keys)}")
    else:
        lines.append("sample_keys: (no fill events in this source)")
    lines.append("")


def autopsy_one(
    tick_dir: Path,
    slug: str,
    *,
    fill: Optional[dict] = None,
) -> Optional[dict]:
    path = tick_dir / f"{slug}.jsonl"
    if not path.is_file():
        return {
            "slug": slug,
            "present": False,
            "n_ticks": 0,
        }
    market = load_market_file(path)
    if market is None:
        return {"slug": slug, "present": True, "n_ticks": 0, "load": "fail"}
    winner = market.winner or infer_winner(market.ticks)
    n_ticks = len(market.ticks)
    held = None
    avg = None
    fill_ts = None
    via = "none"
    if fill:
        held = fill.get("leg")
        avg = fill.get("avg")
        fill_ts = clamp_fill_ts(fill.get("ts"), market.start_ts, market.end_ts)
        via = str(fill.get("event") or "fill")
    if held not in ("up", "down") or fill_ts is None or not avg:
        hit = synthetic_hit(market.ticks, winner)
        if hit is not None:
            held = hit["leg"]
            avg = float(hit["ask"])
            fill_ts = float(hit["ts"])
            via = via + "+synthetic" if fill else "synthetic_75_90"
        else:
            return {
                "slug": slug,
                "present": True,
                "n_ticks": n_ticks,
                "winner": winner,
                "via": via,
                "held": held,
                "no_hit": True,
            }
    stats = book_stats_after(market.ticks, held, fill_ts)
    grid = persist_grid_rows(market.ticks, held, fill_ts, avg, winner)
    live5 = next(
        row for row in grid if row["persist_s"] == 5.0 and row["gui"] is True
    )
    return {
        "slug": slug,
        "present": True,
        "n_ticks": n_ticks,
        "winner": winner,
        "held": held,
        "avg": avg,
        "fill_ts": fill_ts,
        "via": via,
        "won": None if winner is None else held == winner,
        **stats,
        "grid": grid,
        "live5_exit": live5.get("exit"),
        "live5_bid": live5.get("exit_bid"),
        "live5_pnl": live5.get("pnl"),
    }


def build_report(repo: Path, out_dir: Path, since_iso: str) -> str:
    since_unix = parse_iso_unix(since_iso)
    tick_dir = repo / "pathlog" / "ticks"
    lines: List[str] = []
    lines.append("last-120 1s tick autopsy")
    lines.append(f"repo={repo} since={since_iso} unix={int(since_unix)}")
    lines.append("")

    if not tick_dir.is_dir():
        lines.append("NO TICK DIR — pathlog/ticks missing")
        return "\n".join(lines) + "\n"

    index = index_tick_headers(tick_dir)
    lines.append(
        f"tick_headers files={index['files']} tokens={len(index['by_token'])} "
        f"cids={len(index['by_cid'])}"
    )
    dens_all = tick_density(tick_dir)
    overlay = overlay_slugs(tick_dir, OVERLAY_START_UNIX)
    dens_overlay = tick_density(tick_dir, overlay)
    dens_loss = tick_density(tick_dir, NAMED_LOSS_SLUGS)
    for label, dens in (
        ("all_5m", dens_all),
        ("overlay", dens_overlay),
        ("named_loss", dens_loss),
    ):
        lines.append(
            f"density {label}: files={dens['n_files']} empty={dens['empty']} "
            f"mean_ticks={dens['mean_ticks']} p50={dens['p50_ticks']} "
            f"p90={dens['p90_ticks']} mean_bytes={dens['mean_bytes']} "
            f"total_bytes={dens['total_bytes']}"
        )
    lines.append(
        f"overlay_clocks_on_disk={len(overlay)} expected_if_no_prune~1106 "
        "(missing left side = prune)"
    )
    present_loss = [s for s in NAMED_LOSS_SLUGS if (tick_dir / f"{s}.jsonl").is_file()]
    missing_loss = [s for s in NAMED_LOSS_SLUGS if s not in present_loss]
    lines.append(f"named_loss present={len(present_loss)} missing={len(missing_loss)}")
    if missing_loss:
        lines.append("missing: " + " ".join(s.split("-")[-1] for s in missing_loss))
    lines.append("")

    journal_primary = default_journal_path(repo)
    journal_paths = iter_rotated_paths(journal_primary)
    log_paths = rotated_paths(repo / "buybot5m.log", backups=3)
    research_paths = rotated_paths(repo / "underlying_research_buy5m.jsonl", backups=4)
    journal_stats = collect_source_stats(journal_paths, since_unix=since_unix)
    log_stats = collect_source_stats(log_paths, since_unix=since_unix)
    research_stats = collect_source_stats(research_paths, since_unix=since_unix)
    print_source_diag(f"journal ({journal_primary.name})", journal_stats, lines)
    print_source_diag("buybot5m.log", log_stats, lines)
    print_source_diag("underlying_research_buy5m.jsonl", research_stats, lines)

    journal_rows = [row for _p, row in iter_jsonl(journal_paths)]
    log_rows = [row for _p, row in iter_jsonl(log_paths)]
    research_rows = [row for _p, row in iter_jsonl(research_paths)]
    fills_research = collect_fills(research_rows, index, since_unix=since_unix)
    fills_journal = collect_fills(journal_rows, index, since_unix=since_unix)
    fills_log = collect_fills(log_rows, index, since_unix=since_unix)
    merged: Dict[str, dict] = {}
    for group, label in (
        (fills_log, "log"),
        (fills_journal, "journal"),
        (fills_research, "research"),
    ):
        for fill in group:
            fill = dict(fill)
            fill["source"] = label
            prev = merged.get(fill["slug"])
            if prev is None or (fill["ts"] or 0) < (prev["ts"] or 0):
                merged[fill["slug"]] = fill
    fills = sorted(merged.values(), key=lambda item: (item["ts"] or 0, item["slug"]))
    no_slug_journal = 0
    for row in journal_rows:
        if str(row.get("event") or "") not in FILL_EVENTS:
            continue
        stamp = event_unix(row)
        if stamp is not None and stamp + 1e-9 < since_unix:
            continue
        if not resolve_slug(row, index):
            no_slug_journal += 1
    lines.append("== fills joined ==")
    lines.append(
        f"research={len(fills_research)} journal={len(fills_journal)} "
        f"log={len(fills_log)} merged_unique_slugs={len(fills)} "
        f"journal_unresolved_no_token_map={no_slug_journal}"
    )
    with_ticks = [f for f in fills if (tick_dir / f"{f['slug']}.jsonl").is_file()]
    lines.append(
        f"merged_with_tick_file={len(with_ticks)} "
        f"merged_no_tick_file={len(fills) - len(with_ticks)}"
    )
    if fills[:3]:
        sample = fills[0]
        lines.append(
            f"sample_fill slug={sample['slug']} event={sample['event']} "
            f"source={sample.get('source')} leg={sample.get('leg')} "
            f"avg={sample.get('avg')} keys={sample.get('source_keys')}"
        )
    lines.append("")

    joined_autopsies = []
    grid_rows: List[dict] = []
    for fill in with_ticks:
        row = autopsy_one(tick_dir, fill["slug"], fill=fill)
        if not row or not row.get("present") or row.get("no_hit"):
            continue
        joined_autopsies.append(row)
        for item in row.get("grid") or []:
            grid_rows.append(
                {
                    **item,
                    "slug": row["slug"],
                    "won": row.get("won"),
                }
            )
    lines.append("== persist CF on joined fills (live 50/52 dump32 fade, no oracle) ==")
    lines.append(f"autopsies={len(joined_autopsies)}")
    lines.extend(summarize_grid(grid_rows))
    lines.append("")
    loser_grid = []
    for row in joined_autopsies:
        if row.get("won") is False or (
            row.get("winner") in ("up", "down") and row.get("held") != row.get("winner")
        ):
            for item in row.get("grid") or []:
                loser_grid.append({**item, "won": False, "winner_dump": item.get("winner_dump")})
    lines.append("-- joined fills that resolved against the held leg --")
    lines.extend(summarize_grid(loser_grid, losers_only=False))
    lines.append("")

    lines.append("== named-loss slug autopsy (synthetic last-120 75-90 if no fill) ==")
    loss_rows = []
    for slug in NAMED_LOSS_SLUGS:
        fill = merged.get(slug)
        row = autopsy_one(tick_dir, slug, fill=fill)
        loss_rows.append(row)
        if not row.get("present"):
            lines.append(f"{slug} MISSING")
            continue
        if row.get("no_hit"):
            lines.append(
                f"{slug} ticks={row.get('n_ticks')} winner={row.get('winner')} "
                "NO last-120 75-90 hit"
            )
            continue
        lines.append(
            f"{slug} ticks={row['n_ticks']} via={row['via']} held={row['held']} "
            f"avg={row['avg']:.3f} winner={row['winner']} won={row.get('won')} "
            f"min_bid={row.get('min_bid')} first50={fmt_unix(row.get('first50'))} "
            f"first32={fmt_unix(row.get('first32'))} "
            f"book_run={row.get('max_book_run_s'):.1f}s "
            f"gui_run={row.get('max_gui_run_s'):.1f}s "
            f"dt={row.get('median_dt_s')} "
            f"live5={row.get('live5_exit')}@{row.get('live5_bid')} "
            f"pnl={row.get('live5_pnl')}"
        )
    lines.append("")
    loss_present = [r for r in loss_rows if r.get("present") and not r.get("no_hit")]
    printed50 = sum(1 for r in loss_present if r.get("first50"))
    printed32 = sum(1 for r in loss_present if r.get("first32"))
    run1 = sum(1 for r in loss_present if (r.get("max_book_run_s") or 0) >= 1)
    run5 = sum(1 for r in loss_present if (r.get("max_book_run_s") or 0) >= 5)
    lines.append(
        f"named_loss_walkable={len(loss_present)} printed_<=50={printed50} "
        f"printed_<=32={printed32} book_run>=1s={run1} book_run>=5s={run5}"
    )
    lines.append("")

    lines.append("== overlay paper first-touch last-120 75-90 (remaining clocks) ==")
    overlay_grid: List[dict] = []
    overlay_hits = 0
    overlay_losers = 0
    overlay_empty = 0
    for slug in overlay:
        market = load_market_file(tick_dir / f"{slug}.jsonl")
        if market is None or not market.ticks:
            overlay_empty += 1
            continue
        if not matches_series(market.series, market.slug, "5m"):
            continue
        winner = market.winner or infer_winner(market.ticks)
        hit = first_entry(
            market.ticks, ask_min=0.75, ask_max=0.90, ttm_min=0.0, ttm_max=120.0,
        )
        if hit is None:
            continue
        fill = simulate_fak_buy(BUDGET, float(hit["ask"]), hit.get("ask_size"))
        if fill["status"] == "zero":
            continue
        overlay_hits += 1
        held = hit["leg"]
        won = None if winner is None else held == winner
        if won is False:
            overlay_losers += 1
        for item in persist_grid_rows(
            market.ticks, held, float(hit["ts"]), float(fill["avg"] or hit["ask"]), winner,
        ):
            overlay_grid.append({**item, "won": won})
    lines.append(
        f"overlay_files={len(overlay)} empty_or_unreadable={overlay_empty} "
        f"first_touch_hits={overlay_hits} resolved_losers={overlay_losers}"
    )
    lines.extend(summarize_grid(overlay_grid))
    lines.append("")
    lines.append(
        "Notes: dump is bid-only at 32¢ (any bag). Persist is 50/52 + optional "
        "tight-mid GUI (held<=52 other>=48 min_edge=5¢). Pathlog has no last-trade "
        "and no Chainlink, so oracle veto is NOT replayed. Persist 5s on a tape "
        "with median_dt >> 1s completes on the next qualifying tick, not wall 5s."
    )
    lines.append("Do not paste live JSON or restart from this report.")
    text = "\n".join(lines) + "\n"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "autopsy.txt").write_text(text, encoding="utf-8")
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Join 5m fills to pathlog ticks (no orders)")
    ap.add_argument(
        "--repo",
        type=lambda value: Path(value).expanduser().resolve(),
        default=REPO,
        help="Repo root with buy/ and pathlog/ticks (cwd if omitted; not this file's directory)",
    )
    ap.add_argument("--out", type=Path, default=Path("/tmp/last120-research"))
    ap.add_argument("--since", default=OVERLAY_SINCE_ISO)
    args = ap.parse_args(argv)
    report = build_report(args.repo.resolve(), args.out, args.since)
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
