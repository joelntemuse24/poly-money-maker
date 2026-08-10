# AGENTS.md — Poly Money Maker

Quick-reference for AI agents working on this codebase.
Full architecture: `TECHNICAL_DESIGN.md`.

## Project at a Glance

**Live on the VM (2026-08-10):** three standalone buy bots. Sell-side and atomic
mint services are inactive / unused.

| Family | Files | Deploy status | Strategy |
|---|---|---|---|
| Buy-side | `buybot.py`, `buybot5m.py`, `buybothourly.py` | **active** | 96–99¢ ask, budgets $21/$8/$24, hedge 65¢ |
| Sell-side | `bot.py`, `bot5m.py`, `bothourly.py` | inactive | Live JSON on disk (3¢/3min, 2¢/150s, 5¢/5min) but units not running |
| Atomic mint | `buy/runner.py` | unused (`polybuy5m` failed) | legacy |

Plus: `sim/` shadow simulator (inactive), `check_book.py` diagnostic.

## File Map

### Critical — changes here affect real money

| File | What it does | Mirrors |
|---|---|---|
| `bot.py` | 15m sell bot (~1444 lines) | `bot5m.py`, `bothourly.py` |
| `bot5m.py` | 5m sell bot (~1397 lines) | `bot.py`, `bothourly.py` |
| `bothourly.py` | Hourly sell bot (~1349 lines) | `bot.py`, `bot5m.py` |
| `buybot.py` | 15m buy bot (~1146 lines) | `buybot5m.py`, `buybothourly.py` |
| `buybot5m.py` | 5m buy bot (~1141 lines) | `buybot.py`, `buybothourly.py` |
| `buybothourly.py` | Hourly buy bot (~1144 lines) | `buybot.py`, `buybot5m.py` |
| `buy/market.py` | MarketGateway + MintMarket dataclass (shared by buy-side bots) | — |
| `buy/runner.py` | Atomic mint entry — **unused / legacy** (separate `.venv-buy`) | — |

**Pattern:** Each bot family has 3 near-identical copies differing only in constants
(SLUG_PREFIX, SLUG_EXCLUDES, buy_budget, buy/sell window, tick_size). A logic change
to one usually needs propagation to its siblings.

### Safe to modify

| File | Purpose |
|---|---|
| `check_book.py` | Diagnostic — inspect a live order book |
| `check_hourly_mint.py` | Diagnostic — verify hourly mint eligibility |
| `backtest_sell_window.py` | Backtest tool — not run in production |
| `sim/` | Shadow simulator — paper-trades only, never places real orders |
| `*.example.json` | Templates — not loaded by bots |

### Read-only / auto-generated — never edit

- `positions*.json`, `pnl*.json` — bot state and P&L (auto-generated)
- `*.log` — structured JSON-line logs with rotation
- `.heartbeat*` — uptime tick counters
- `buy_data*/` — atomic mint runtime data (legacy; unused)
- `strategy*.json` (without `.example`) — live configs, hot-reloaded
- `.env` — secrets (gitignored)

## Never Do

- **Never delete or truncate state files** (`positions*.json`, `pnl*.json`) — the
  bots rely on them to track entries, hedges, and P&L across restarts.
- **Never modify `buy/relayer.py` or `buy/contracts.py` without extreme care** —
  these construct on-chain calldata. A wrong byte will cause reverts costing gas.
- **Never change `SLUG_PREFIX` or `SLUG_EXCLUDES` without updating all three bots
  in the family** — mismatched exclusions cause cross-bot interference.
- **Never run a bot with `dry_run: false` against production unless you mean it** —
  there is no confirmation prompt.
- **Never commit `strategy*.json` (live configs), `.env`, or state files** — they
  are gitignored but double-check before `git add .`.

## How to Verify a Change

### Buy-side bots

```bash
# Dry-run (no real orders) — safe to run anytime
python buybot.py        # 15m, uses strategy_buy.json
python buybot5m.py      # 5m,  uses strategy_buy5m.json
python buybothourly.py  # hr,  uses strategy_buyhourly.json
```

Watch the console for `[DRY BUY]` / `[DRY SELL]` markers. Ctrl-C to stop.

### Sell-side bots

```bash
python bot.py           # 15m, uses strategy.json
python bot5m.py         # 5m,  uses strategy5m.json
python bothourly.py     # hr,  uses strategy_hourly.json
```

### Quick order book check

```bash
python check_book.py
```

### What to look for

- `[DRY BUY]` / `[DRY SELL]` in dry-run — confirms trigger logic fires
- `cycle_error` in logs — indicates an unhandled exception (bot survives but logs it)
- `hedge_attempt` / `hedge_fill` — hedge logic triggered
- `buy_skip_ambiguous` — GUI display prices too close
- `buy_skip_no_consensus` — ask in band but Polymarket GUI prices don't show a clear winner/loser
- `buy_skip_incomplete_book` — missing GUI price on one or both legs (no mid and no last trade)

## Key Conventions

- **All bots are single-file polling loops** — no async, no modules, no database.
  Section comment banners (`# --- PRICING ---`) act as visual boundaries.
- **Hot-reload:** Strategy JSON files are re-read every cycle via `load_strategy()`.
  Changes take effect on the next tick — no restart needed for sell/buy bots.
- **Atomic save:** State files use `atomic_save()` (write to `.tmp`, then `os.replace`).
  PNL files also use `atomic_save()` as of 2026-08-09.
- **FAK orders only:** All orders are Fill-And-Kill — no resting orders, no market making.
- **Hedge is sell-only exit:** Buy-side bots never profit-take. The only sell path is
  the hedge (bid ≤ 65¢). Sell-side bots sell the loser leg at threshold, hedge
  reversals, and redeem.
- **Tick sizes:** 5m markets use `0.001`, 15m and hourly use `0.01`.
- **One entry per market:** Enforced via `meta.get("bought_token")` in state cache.
- **Notifications:** Fire-and-forget via ntfy.sh. All bots share topic
  `polybot-joel-btc` by default. Never let notification failures crash the bot.

## Landmines

1. **The two buy-side systems are completely different (mint unused):**
   - Standalone bots (`buybot*.py`) buy the winning leg at 96–99¢ via FAK on CLOB — **this is the live entry path**.
   - Atomic mint (`buy/runner.py`) mints complete sets at $1.00 via on-chain relayer — **not in active use**.
   - They share NO code, NO state, and use DIFFERENT venvs (`.venv` vs `.venv-buy`).

2. **The three bots in each family are near-identical copies, not shared modules.**
   A bug fix in `buybot.py` probably also exists in `buybot5m.py` and `buybothourly.py`.

3. **`bothourly.py` (sell-side) discovers markets differently** from the other sell
   bots — it uses the Data API positions endpoint + slug prefix filtering, not
   Gamma API discovery.

4. **The 5m buy bot uses seconds-based window checks** (`BUY_START_S = 90`) while
   the 15m and hourly buy bots use minutes-based (`BUY_WINDOW_MIN = 3.0 / 5.0`).
   Don't mix them up when propagating changes.

5. **PNL fallback in GC assumes par redemption** — if `pnl_redeem_value == 0` and
   `bought_size > 0`, the GC code sets `redeem_value = bought_size` (full $1.00 per
   share). This is correct when the bot buys the winning leg but would overstate P&L
   if it ever bought the wrong leg.

6. **No `if __name__ == "__main__"` guard** — these scripts execute at module level.
   They cannot be imported.

## Systemd Services (in `deploy/`)

| Service file | Bot | Type |
|---|---|---|
| `polybot5m.service` | `bot5m.py` | 5m sell |
| `polybot-hourly.service` | `bothourly.py` | hourly sell |
| `polybuybot.service` | `buybot.py` | 15m buy |
| `polybuybot5m.service` | `buybot5m.py` | 5m buy |
| `polybuybothourly.service` | `buybothourly.py` | hourly buy |

Note: `polybot` (15m sell) service file is VM-only. Atomic mint services
(`polybuy*`, armers) are also VM-only and **unused**.

## Dependencies

```
py_clob_client_v2   # Polymarket CLOB SDK
requests            # HTTP for Data API, Gamma API, ntfy
python-dotenv       # .env loading
rich                # Terminal UI (tables, panels)
web3, eth-account, eth-abi, eth-utils  # On-chain redeem + atomic mint
```
