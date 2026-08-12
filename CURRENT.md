# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-12** — revert to **$2.50 / 98–99¢** CLOB buys; mint paused;
pathlog records books for entry backtests.

---

## What we’re doing

**Mint-only helper is paused.** Stop `polymintbot` and leave it disabled. Do not
mint complete sets. Operator still sells leftover mint inventory by hand.

**Active strategy:** the three CLOB buy bots, restored to the pre-widen probe:

| Knob | Value |
|---|---|
| `buy_budget` | **$2.50** / market |
| Ask band | **98–99¢** (`buy_threshold` 0.98, `buy_max_price` 0.99) |
| GUI consensus | winner ≥ 92¢, loser ≤ 10¢ |
| Windows | 5m **90 s** · 15m **3.0 min** · hourly **4.0 min** |
| Hedge | bid ≤ **65¢** and ask ≤ **70¢**, spread ≤ 15¢ |
| Underlying edge | **$0** (direction still required) |
| `max_open_positions` | **0 = unlimited** |
| `toxic_force_exit_below` | **90¢** |

Safety code from later PRs stays (hedge book integrity, toxic-fill dump,
ambiguous-buy quarantine, WS books). Only the **knobs** revert — not those
fail-closed fixes.

**Also running (no orders):** `pathlog.py` (`polypathlog`) writes one JSONL file
per market under `pathlog/ticks/` so we can ask “if we had bought at 80¢ with
2 minutes left, would we have won?”

---

## Pathlog / backtest

Recorder samples CLOB top-of-book ~1/s in the late window (whole 5m; last 8m of
15m; last 15m of hourly). After expiry it stamps `winner` from Gamma.

On the VM:

```bash
# install + start recorder (once)
sudo cp deploy/polypathlog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polypathlog
journalctl -u polypathlog -f

# after some markets have resolved:
python check_path_backtest.py --ask-min 0.80 --ask-max 0.85 --ttm-max 120 --budget 2.5
python check_path_backtest.py --grid --budget 2.5
python check_path_backtest.py --export-market btc-updown-5m-1786528500 --csv /tmp/m.csv
python check_path_backtest.py --ask-min 0.98 --ask-max 0.99 --ttm-max 90 --csv /tmp/hits.csv
```

`--grid` is the Excel-shaped table: ask × seconds-left, hit count, win rate,
hypothetical $2.50 PnL (win = redeem $1, loss = −$2.50, no hedge model).

Kill switch: `touch STOP_PATHLOG`.

---

## Ops

- **VM:** `~/poly-money-maker` on `instance-20260516-185922`.
- **Mint:** `sudo systemctl stop polymintbot && sudo systemctl disable polymintbot`
- **Buy bots — live JSON must be edited.** Code defaults are 98–99 / $2.50, but
  `strategy_buy*.json` on disk overlays them. Copy examples or patch knobs:
  ```bash
  cd ~/poly-money-maker && git pull
  # review, then overlay (keeps gitignored live files):
  python3 - <<'PY'
  import json, pathlib
  for name in ("strategy_buy.json","strategy_buy5m.json","strategy_buyhourly.json"):
      p = pathlib.Path(name)
      if not p.exists():
          continue
      d = json.loads(p.read_text())
      d.update({
          "buy_threshold": 0.98, "buy_max_price": 0.99,
          "min_winner_bid": 0.92, "max_loser_bid": 0.10,
          "min_underlying_edge_usd": 0.0, "toxic_force_exit_below": 0.90,
          "hedge_threshold": 0.65, "hedge_require_ask_max": 0.70,
          "buy_budget": 2.5,
      })
      if "buy_start_s" in d:
          d["buy_start_s"] = 90
      if "buy_window_min" in d:
          d["buy_window_min"] = 3.0 if "hourly" not in name else 4.0
      p.write_text(json.dumps(d, indent=2) + "\n")
      print("updated", name)
  PY
  sudo systemctl restart polybuybot polybuybot5m polybuybothourly
  ```
  Confirm `dry_run` / `entry_enabled` in those files before restarting live.
- **Pathlog:** start `polypathlog` as above (no `.env` required).

---

## Open / next

- [x] Pause minting; restore $2.50 / 98–99¢ CLOB probe knobs.
- [x] Path recorder + `check_path_backtest.py` (first-touch ask × time-left).
- [ ] On VM: stop mint, overlay live strategy JSON, restart buy bots + pathlog.
- [ ] Let pathlog collect resolved markets, then run `--grid` before changing bands again.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing mint/buy/hedge logic.
2. Do **not** restart minting unless the operator asks.
3. Never truncate state/PnL/log/pathlog files; never commit live strategy/state/`.env`.
4. When an ops decision changes, **update this file in the same PR/commit**.
