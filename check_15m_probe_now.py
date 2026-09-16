#!/usr/bin/env python3
"""Now-snapshot: would the 15m $5 dry-run probe fire on the current clock?

Public Gamma + CLOB GET only. Does not import buybot.py, does not read .env,
does not POST orders. Default strategy is strategy_buy15m_probe.example.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from buy.market import MarketGateway
from buy.probe_15m import (
    ask_in_band,
    in_buy_window,
    live_posting_armed,
    probe_live_flip_note,
    probe_spend_usd,
    should_evaluate_entries,
)
from buy.strategy_coherence import validate_15m_strategy_coherence

REPO = Path(__file__).resolve().parent
GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SERIES = "btc-up-or-down-15m"
DEFAULT_STRATEGY = REPO / "strategy_buy15m_probe.example.json"


def _best_level(levels, *, side: str) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(levels, list) or not levels:
        return None, None
    priced = []
    for row in levels:
        if not isinstance(row, dict):
            continue
        try:
            px = float(row.get("price"))
            sz = float(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if px != px or sz != sz:
            continue
        priced.append((px, sz))
    if not priced:
        return None, None
    if side == "ask":
        px, sz = min(priced, key=lambda item: item[0])
    else:
        px, sz = max(priced, key=lambda item: item[0])
    return px, sz


def _book(token_id: str) -> Dict[str, Any]:
    response = requests.get(
        f"{CLOB}/book",
        params={"token_id": token_id},
        timeout=10,
        headers={"User-Agent": "poly-money-maker-15m-probe-now/1.0"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return {}
    ask, ask_sz = _best_level(payload.get("asks") or [], side="ask")
    bid, bid_sz = _best_level(payload.get("bids") or [], side="bid")
    return {"bid": bid, "bid_sz": bid_sz, "ask": ask, "ask_sz": ask_sz}


def _load_strategy(path: Path) -> Dict[str, Any]:
    overrides = json.loads(path.read_text())
    if not isinstance(overrides, dict):
        raise ValueError("strategy root must be an object")
    cfg = dict(overrides)
    validate_15m_strategy_coherence(cfg)
    return cfg


def _decide(cfg: Dict[str, Any], seconds_left: float, up: Dict[str, Any], dn: Dict[str, Any]) -> Dict[str, Any]:
    window_min = float(cfg.get("buy_window_min") or 0)
    lo = float(cfg.get("buy_threshold") or 0)
    hi = float(cfg.get("buy_max_price") or 0)
    max_spread = float(cfg.get("max_entry_spread") or 0)
    dry = bool(cfg.get("dry_run", True))
    armed = bool(cfg.get("entry_enabled", False))
    spend = probe_spend_usd(
        float(cfg.get("buy_budget") or 0),
        float(cfg.get("buy_max_spend") or 0),
        float(cfg.get("market_spend_cap") or 0),
    )
    window_ok = in_buy_window(seconds_left, window_min)
    up_ok = ask_in_band(up.get("ask"), lo, hi)
    dn_ok = ask_in_band(dn.get("ask"), lo, hi)

    def _spread(side: Dict[str, Any]) -> Optional[float]:
        bid, ask = side.get("bid"), side.get("ask")
        if bid is None or ask is None:
            return None
        return float(ask) - float(bid)

    up_spread = _spread(up)
    dn_spread = _spread(dn)
    up_tight = up_spread is not None and up_spread <= max_spread + 1e-12
    dn_tight = dn_spread is not None and dn_spread <= max_spread + 1e-12
    would = bool(
        window_ok
        and should_evaluate_entries(dry, armed)
        and ((up_ok and up_tight) or (dn_ok and dn_tight))
    )
    reasons = []
    if not window_ok:
        reasons.append("outside_buy_window")
    if not (up_ok or dn_ok):
        reasons.append("ask_not_in_ge95_band")
    if (up_ok and not up_tight) or (dn_ok and not dn_tight):
        reasons.append("wide_entry_spread")
    if not should_evaluate_entries(dry, armed):
        reasons.append("entries_not_evaluated")
    if live_posting_armed(dry, armed):
        reasons.append("LIVE_POSTING_ARMED")
    return {
        "window_ok": window_ok,
        "up_ask_in_band": up_ok,
        "dn_ask_in_band": dn_ok,
        "up_spread": up_spread,
        "dn_spread": dn_spread,
        "would_log_buy": would,
        "live_posting": live_posting_armed(dry, armed),
        "spend_usd": spend,
        "reasons": reasons,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--strategy",
        default=str(DEFAULT_STRATEGY),
        help="probe JSON (default: strategy_buy15m_probe.example.json)",
    )
    args = ap.parse_args(argv)
    path = Path(args.strategy)
    cfg = _load_strategy(path)
    gateway = MarketGateway(gamma_url=GAMMA, data_api_url=DATA_API, timeout=15.0)
    markets = gateway.discover([SERIES])
    now = time.time()
    live = [
        m for m in markets
        if m.active and not m.closed and not m.neg_risk and m.end_ts > now
    ]
    live.sort(key=lambda m: m.end_ts)
    print(f"strategy={path.name}")
    print(
        f"dry_run={cfg.get('dry_run')} entry_enabled={cfg.get('entry_enabled')} "
        f"live_posting={live_posting_armed(bool(cfg.get('dry_run')), bool(cfg.get('entry_enabled')))}"
    )
    print(
        f"band={cfg.get('buy_threshold')}–{cfg.get('buy_max_price')} "
        f"persist_min={cfg.get('entry_persist_min_price', 0.95)} "
        f"window={cfg.get('buy_window_min')}m "
        f"oracle=${cfg.get('min_underlying_edge_usd')} "
        f"spend=${probe_spend_usd(cfg['buy_budget'], cfg['buy_max_spend'], cfg.get('market_spend_cap') or 0):.2f}"
    )
    if not live:
        print("no active btc-up-or-down-15m market")
        for line in probe_live_flip_note():
            print(f"later_live: {line}")
        return 0
    market = live[0]
    seconds_left = market.end_ts - now
    up = _book(market.up_token)
    dn = _book(market.dn_token)
    decision = _decide(cfg, seconds_left, up, dn)
    print(f"slug={market.slug}")
    print(f"question={market.question}")
    print(f"ttm_s={seconds_left:.1f}")
    print(
        f"up bid/ask={up.get('bid')}/{up.get('ask')} "
        f"dn bid/ask={dn.get('bid')}/{dn.get('ask')}"
    )
    print(
        f"would_log_buy={decision['would_log_buy']} "
        f"window_ok={decision['window_ok']} "
        f"up_band={decision['up_ask_in_band']} dn_band={decision['dn_ask_in_band']}"
    )
    if decision["reasons"]:
        print("reasons=" + ",".join(decision["reasons"]))
    if decision["live_posting"]:
        print("REFUSING: strategy would arm live POSTs — probe must stay dry_run", file=sys.stderr)
        return 2
    for line in probe_live_flip_note():
        print(f"later_live: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
