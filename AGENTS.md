# AGENTS.md — Poly Money Maker

Quick-reference for AI agents working on this codebase.
Full architecture: `TECHNICAL_DESIGN.md`.
**Live probe / ops decisions:** read `CURRENT.md` first (update it when strategy changes).

## Project at a Glance

**Live on the VM:** three standalone buy bots — nothing else. They buy the winning
leg of Polymarket BTC "Up or Down" markets at 98–99¢ in the final window, hedge at
65¢ on reversal, and redeem winners at $1.00. See `CURRENT.md` for the active
probe budget, edge/toxic knobs, and near-term goals.

| File | Service | Markets | Oracle | Budget | Window |
|---|---|---|---|---|---|
| `buybot.py` | `polybuybot` | 15m | Chainlink TWAP 60s | $5 | final 3.0 min |
| `buybot5m.py` | `polybuybot5m` | 5m | Chainlink TWAP 30s | $5 | final 90 s |
| `buybothourly.py` | `polybuybothourly` | hourly | Binance BTCUSDT | $5 | final 4.0 min |

Plus: `check_book.py` diagnostic.

## File Map

### Critical — changes here affect real money

| File | What it does | Mirrors |
|---|---|---|
| `buybot.py` | 15m buy bot (~1716 lines) | `buybot5m.py`, `buybothourly.py` |
| `buybot5m.py` | 5m buy bot (~1705 lines) | `buybot.py`, `buybothourly.py` |
| `buybothourly.py` | Hourly buy bot (~1707 lines) | `buybot.py`, `buybot5m.py` |
| `buy/market.py` | MarketGateway — Gamma discovery + market metadata | — |
| `buy/btc_price.py` | Resolution-aligned BTC feeds (TWAP 30/60, Binance) + PTB capture | — |
| `buy/clob_book_ws.py` | CLOB market-channel WS top-of-book (buy/hedge speed path) | — |

**Pattern:** The three bots are near-identical copies differing only in constants
(SLUG_PREFIX, SLUG_EXCLUDES, oracle source, buy budget/window, tick_size). A logic
change to one usually needs propagation to its siblings.

### Safe to modify

| File | Purpose |
|---|---|
| `check_book.py` | Diagnostic — inspect a live order book |
| `check_edge_counterfactual.py` | Diagnostic — resolution win rate if edge skips had filled |
| `CURRENT.md` | Living ops/probe status — update when decisions change |
| `strategy_buy*.example.json` | Config templates — not loaded by bots |

### Read-only / auto-generated — never edit

- `positions_buy*.json`, `pnl_buy*.json` — bot state and P&L (auto-generated)
- `*.log` — structured JSON-line logs with rotation
- `.heartbeat*` — uptime tick counters
- `underlying_research_buy*.jsonl`, `ptb_*_buy*.json` — oracle/PTB decision audit
- `strategy_buy*.json` (without `.example`) — live configs, hot-reloaded
- `.env` — secrets (gitignored)

## Never Do

- **Never delete or truncate state files** (`positions_buy*.json`, `pnl_buy*.json`,
  logs, heartbeats, research/PTB files) — the bots rely on them to track entries,
  hedges, and P&L across restarts.
- **Never change `SLUG_PREFIX` or `SLUG_EXCLUDES` without checking all three bots** —
  `btc-updown` is a prefix of `btc-updown-5m`; mismatched exclusions cause
  cross-bot interference on the same market.
- **Never run a bot with `dry_run: false` against production unless you mean it** —
  there is no confirmation prompt.
- **Never commit live `strategy_buy*.json`, `.env`, or state files** — they are
  gitignored but double-check before `git add .`.

## How to Verify a Change

```bash
# First verify the selected strategy file has dry_run=true and entry_enabled=false.
# Never assume these commands are dry-run merely because they are launched manually.
python buybot.py        # 15m, uses strategy_buy.json
python buybot5m.py      # 5m,  uses strategy_buy5m.json
python buybothourly.py  # hr,  uses strategy_buyhourly.json

python check_book.py    # quick order book check
```

Watch the console for `[DRY BUY]` / `[DRY SELL]` markers. Ctrl-C to stop.

### What to look for

- `[DRY BUY]` / `[DRY SELL]` in dry-run — confirms trigger logic fires
- `cycle_error` in logs — unhandled exception (bot survives but logs it)
- `hedge_attempt` / `hedge_fill` — hedge fired after force-fresh REST + book integrity
- `hedge_skip_toxic_book` — bid dipped but ask/spread still say "not reversed"
- `hedge_skip_incomplete_rest` — REST missing a side; fail closed (no WS sell)
- `buy_skip_ambiguous` — GUI display prices too close
- `buy_skip_no_consensus` — ask in band but GUI/tight-book gate failed
- `buy_fill_below_band` — fill avg below band; inventory persisted + `toxic_fill` force-exit
- `buy_ghost_fill` — balance reconciliation after null/delayed BUY confirm
- `buy_uncertain` — POST outcome unresolved; durable token/baseline quarantine blocks re-buy
- `buy_skip_incomplete_book` — missing GUI price on a leg (no mid and no last trade)
- `buy_skip_underlying_edge` — live oracle not ≥ `min_underlying_edge_usd` ($5 on 5m, $10 on 15m/hourly) from PTB
- `buy_skip_underlying_side` — book wants the opposite leg from the underlying move

## Key Conventions

- **All bots are single-file polling loops** — no `asyncio` or database. Small
  thread pools isolate book, refresh, notification, and redemption-status I/O.
  Section comment banners (`# --- PRICING ---`) act as visual boundaries.
- **Hot-reload:** Strategy JSON files are re-read every cycle via `load_strategy()`.
  Changes take effect on the next tick — no restart needed.
- **Atomic durable save:** State and P&L files flush + `fsync` the `.tmp`, replace
  it, then `fsync` the parent directory before an order can proceed.
- **Fail-closed startup:** A valid strategy file is required at startup. A
  missing/malformed hot reload disables new entries while retaining the
  last-known-good hedge parameters. A per-bot process lock prevents duplicate
  live instances.
- **FAK orders only:** All orders are Fill-And-Kill — no resting orders, no market
  making.
- **Hedge is sell-only exit:** The bots never profit-take. The only sell path is the
  hedge (bid ≤ 65¢ **and** ask ≤ 70¢ with tight spread, force-fresh REST
  fail-closed); everything else rides to redemption at $1.00. WS may *arm*
  a hedge check; selling requires two-sided REST integrity on every attempt.
- **Tick sizes:** 5m markets use `0.001`, 15m and hourly use `0.01`.
- **One entry per market:** Enforced via `meta.get("bought_token")` in state cache.
- **Notifications:** Fire-and-forget via ntfy.sh (topic `polybot-joel-btc`).
  Never let notification failures crash the bot.

## Landmines

1. **The three bots are near-identical copies, not shared modules.** A bug fix in
   `buybot.py` probably also applies to `buybot5m.py` and `buybothourly.py`.
2. **The 5m bot uses seconds-based window checks** (`buy_start_s = 90`) while the
   15m and hourly bots use minutes (`buy_window_min = 3.0 / 4.0`). Don't mix them
   when propagating changes. The 5m loop must define `seconds_left` (not only
   `minutes_left`) or it NameErrors every cycle.
3. **Settlement is confirmation-gated.** A relayer submission is not P&L.
   Redemption is credited only after relayer confirmation and a complete Data API
   snapshot shows the inventory gone; GC never invents par value.
4. **No `if __name__ == "__main__"` guard** — these scripts execute at module level.
   They cannot be imported.

## Systemd Services (in `deploy/`)

| Service file | Bot |
|---|---|
| `polybuybot.service` | `buybot.py` (15m) |
| `polybuybot5m.service` | `buybot5m.py` (5m) |
| `polybuybothourly.service` | `buybothourly.py` (hourly) |

CI: pushes to `main` touching the buy bots, `buy/`, or `requirements.txt` deploy to
the VM via SSH (`git pull` + `pip install` only). Services are **not** auto-restarted —
start/restart deliberately after validation (`systemctl start` / `restart`).

## Dependencies

```
py_clob_client_v2   # Polymarket CLOB SDK
py-builder-relayer-client  # Relayer proxy transaction builder/signing
py-builder-signing-sdk     # POLY_BUILDER_* HMAC authentication headers
requests            # HTTP for Data API, Gamma API, ntfy, relayer
python-dotenv       # .env loading
rich                # Terminal UI (tables, panels)
eth-abi, eth-utils, eth-account  # Redeem calldata encoding
websocket-client    # CLOB market-channel WS (buy/clob_book_ws.py)
web3                # transitive of the CLOB client stack
```
