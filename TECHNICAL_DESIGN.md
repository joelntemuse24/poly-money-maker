# Poly Money Maker — Technical Design Document

> **Audience:** An engineer who can read Python and needs to understand, operate, or
> modify the live system. This document describes **only what runs in production**:
> three standalone buy-side bots. Everything else has been removed from the repo.

---

## Table of Contents

1. [What the System Does](#1-what-the-system-does)
2. [The Live System at a Glance](#2-the-live-system-at-a-glance)
3. [Architecture](#3-architecture)
4. [Market Discovery](#4-market-discovery)
5. [Underlying Oracle Feeds & PTB](#5-underlying-oracle-feeds--ptb)
6. [The Buy Decision](#6-the-buy-decision)
7. [The Hedge (Sell-Only Exit)](#7-the-hedge-sell-only-exit)
8. [Redemption](#8-redemption)
9. [State, P&L, and Garbage Collection](#9-state-pl-and-garbage-collection)
10. [Configuration & Hot Reload](#10-configuration--hot-reload)
11. [Operations on the VM](#11-operations-on-the-vm)
12. [Error Handling Philosophy](#12-error-handling-philosophy)
13. [Latency & Optional AI Validation](#13-latency--optional-ai-validation)
14. [Landmines](#14-landmines)
15. [Removed / Historical](#15-removed--historical)
16. [Glossary](#16-glossary)

---

## 1. What the System Does

Poly Money Maker trades Polymarket's Bitcoin **"Up or Down"** binary markets. Each
market asks: will BTC be up or down at the end of the window (5 minutes, 15 minutes,
or 1 hour)? A market has two legs (UP and DOWN). Exactly one leg wins and redeems at
$1.00; the other redeems at $0.

**The strategy:** enter earlier and more often — once the winning leg's ask is in
the **75–90¢** band inside each bot's buy window — with a Fill-And-Kill (FAK)
market order, then redeem winners at $1.00. The probe deliberately trades some
extra reversal risk for higher market participation (missing markets made it hard
to earn back losses from the few hedges that fired).

**The risk:** BTC can reverse after entry. If the leg we bought truly collapses
(bid **and** ask both drop — not a lone 1¢ bid under a still-high ask), the bot
**hedges**: it market-sells the held leg when bid ≤ 35¢ (and ask ≤ 40¢ with a
tight spread). After that check, the FAK follows the **live bid** (a 20¢ print
on a still-tight book is a fill, not $0). There is no profit-taking
sell — the only sell path is the hedge; everything else rides to redemption.

**Economics per share (no reversal):** buy at ~75–90¢, redeem at $1.00 → ~10–25¢
gross (wider band than the old 98–99¢ probe; more fill opportunity, more reversal
exposure).
**Economics per share (hedged reversal):** buy at ~75–90¢, sell at the collapsed
bid → bounded loss instead of riding a wrong side to $0.

---

## 2. The Live System at a Glance

Three standalone Python scripts run as systemd services on a single GCP VM. Each
trades a different market cadence and uses a different resolution oracle.

| | 15m | 5m | Hourly |
|---|---|---|---|
| Script | `buybot.py` | `buybot5m.py` | `buybothourly.py` |
| Service | `polybuybot` | `polybuybot5m` | `polybuybothourly` |
| Series slug | `btc-up-or-down-15m` | `btc-up-or-down-5m` | `btc-up-or-down-hourly` |
| Slug prefix / excludes | `btc-updown` (excl. `btc-updown-5m`, `bitcoin-up-or-down`) | `btc-updown-5m` (excl. `bitcoin-up-or-down`) | `bitcoin-up-or-down` (excl. `btc-updown`, `btc-updown-5m`) |
| Resolution oracle | Chainlink BTC TWAP 60s | Chainlink BTC TWAP 30s | Binance BTCUSDT |
| Buy window | final 4.0 min (`buy_window_min`) | final 120 s (`buy_start_s`) | final 13.0 min (`buy_window_min`) |
| Ask band | 75–90¢ | 75–90¢ | 75–90¢ |
| Budget / market | $2.50 USDC | $2.50 USDC | $2.50 USDC |
| Hedge trigger | bid ≤ 35¢ **and** ask ≤ 40¢, spread ≤ 15¢ | same | same |
| Tick size | 0.01 | 0.001 | 0.01 |
| Strategy file | `strategy_buy.json` | `strategy_buy5m.json` | `strategy_buyhourly.json` |
| State file | `positions_buy.json` | `positions_buy5m.json` | `positions_buyhourly.json` |
| P&L file | `pnl_buy.json` | `pnl_buy5m.json` | `pnl_buyhourly.json` |
| Log | `buybot.log` | `buybot5m.log` | `buybothourly.log` |
| Heartbeat | `.heartbeat_buy` | `.heartbeat_buy5m` | `.heartbeat_buyhourly` |
| Research log | `underlying_research_buy.jsonl` | `underlying_research_buy5m.jsonl` | `underlying_research_buyhourly.jsonl` |
| PTB store | `ptb_twap60_buy.json` | `ptb_twap30_buy5m.json` | `ptb_binance_buyhourly.json` |

All strategy/state/log files live in the repo working directory on the VM
(`/home/ntemusejoel/poly-money-maker`). State, logs, and live strategy JSONs are
gitignored and must never be committed or deleted.

---

## 3. Architecture

```
+-------------------+   +-------------------+   +-------------------+
| buybot.py (15m)   |   | buybot5m.py (5m)  |   | buybothourly (hr) |
+--------+----------+   +--------+----------+   +--------+----------+
         |                     |                       |
         +----------+----------+----------+------------+
                    |                     |
          +---------v---------+   +-------v------------------+
          | buy/market.py     |   | buy/btc_price.py         |
          | Gamma discovery + |   | RTDS oracle streams +    |
          | market metadata   |   | PTB capture              |
          +-------------------+   +--------------------------+
                    |
          +---------v---------+
          | buy/clob_book_ws.py |
          | CLOB WS top-of-book |
          +-------------------+
                    |
     Polymarket CLOB (FAK orders) / Data API (positions) / Relayer (redeem)
```

**Design rules:**

- **Single-file bots.** Each bot is one self-contained polling loop — no `asyncio`,
  database, or message queue. Small thread pools isolate refresh, book,
  notification, entry, and redemption-status I/O. Section banners
  (`# --- PRICING ---`) are the visual structure. The three bots are near-identical
  copies that differ only in constants (slug prefix/excludes, oracle source,
  window, budget, tick size). A logic change in one almost always needs to be
  propagated to the other two.
- **Shared helpers live in `buy/`.** Only three modules are used by the live bots:
  - `buy/market.py` — `MarketGateway`: Gamma API discovery and market metadata
    (token IDs, tick size, end dates).
  - `buy/btc_price.py` — resolution-aligned BTC price feeds streamed over
    Polymarket's RTDS websocket, plus Price-To-Beat (PTB) capture and the
    `append_research` audit logger.
  - `buy/clob_book_ws.py` — CLOB market-channel websocket top-of-book cache, the
    low-latency quote source for the buy and hedge hot paths.
- **No `if __name__ == "__main__"` guard.** The scripts execute at module level; they
  cannot be imported. Tests therefore don't import them.
- **JSON files are the database.** Positions, P&L, heartbeat, and config are JSON
  files in the working directory. `atomic_save()` writes and `fsync`s `.tmp`, keeps
  a validated `.bak`, replaces the primary, then `fsync`s the directory. Non-finite
  JSON is rejected.
- **FAK orders only.** All orders are Fill-And-Kill: fill immediately at the top of
  book or die. No resting orders, no market making.

---

## 4. Market Discovery

Each cycle, the bot asks `MarketGateway` (`buy/market.py`) for active markets in its
series via the Gamma API (`https://gamma-api.polymarket.com`), filtered by
`SLUG_PREFIX`/`SLUG_EXCLUDES` so the three bots never touch the same market:

- `buybot.py`: prefix `btc-updown`, excluding `btc-updown-5m` and
  `bitcoin-up-or-down` (the 5m and hourly families share substrings, so the excludes
  are load-bearing).
- `buybot5m.py`: prefix `btc-updown-5m`, excluding `bitcoin-up-or-down`.
- `buybothourly.py`: prefix `bitcoin-up-or-down`, excluding both `btc-updown`
  families.

Discovery yields the condition ID, the UP/DOWN CLOB token IDs, the tick size, and
the market end time. The bot also tracks its own holdings via the Data API
positions endpoint so it can manage hedges and redemptions across restarts.

**Never change `SLUG_PREFIX`/`SLUG_EXCLUDES` in one bot without checking the other
two** — overlapping prefixes cause two bots to trade the same market.

---

## 5. Underlying Oracle Feeds & PTB

Each market resolves against a specific oracle, and each bot streams **that same
oracle** so its decisions align with the resolution data:

| Bot | Oracle | Source constant |
|---|---|---|
| 15m | Chainlink BTC/USD, TWAP 60s | `SOURCE_TWAP_60` |
| 5m | Chainlink BTC/USD, TWAP 30s | `SOURCE_TWAP_30` |
| Hourly | Binance BTCUSDT spot | `SOURCE_BINANCE` |

`buy/btc_price.py` maintains a background websocket to Polymarket's RTDS feed and
exposes `get_btc_feed(source)`.

**Price To Beat (PTB):** each market resolves by comparing the oracle price at
window close against the price at window open — the PTB. The bot captures and caches
the PTB per market (`ptb_*_buy*.json`) so it can compare *live* oracle price vs PTB
at decision time.

**Research audit:** every PTB capture and every buy decision (taken or skipped) is
appended to `underlying_research_buy*.jsonl` via `append_research()`. These files
are the ground truth for post-hoc accuracy analysis; they are gitignored.

---

## 6. The Buy Decision

A buy fires only when **all** of these gates pass, evaluated in the buy window
(4.0 min / 120 s / 13.0 min before close):

1. **Window.** Market is inside the buy window and past the `buy_grace_s` buffer; the
   bot has not already entered this market (`one_entry_per_market`, enforced via
   `meta["bought_token"]` in state), and the `buy_cooldown_s` per-market cooldown has
   elapsed.
2. **GUI consensus.** The Polymarket UI display price (mid when spread ≤ 10¢, else
   last trade) must show a clear winner and loser: winner ≥ `min_winner_bid` (70¢),
   loser ≤ `max_loser_bid` (30¢), and the gap ≥ `min_bid_edge` (5¢).
3. **Tight real book (critical).** The winning leg's **REST** top-of-book must have
   bid ≥ `min_winner_bid` and `ask − bid` ≤ `max_entry_spread` (5¢). A high ask over
   a 1¢ bid is a fake price — last-trade GUI can still look like a winner while
   there is no real bid under the ask. WS quotes alone are never used to arm entry.
4. **Ask band / 75¢ trigger.** The best ask of the winning leg is within
   `buy_threshold`–`buy_max_price` (75–90¢). **75¢ is the trigger**, not a target
   mid-band: the bot buys as soon as ask ≥ 75¢ (other gates passing) and posts the
   FAK at the *live* ask — so catching the print early yields fills near 75¢.
   90¢ is only a hard ceiling (never enter if ask is already above it).
5. **Underlying gate.** If `underlying_gate_enabled`, the live oracle price must be
   at least `min_underlying_edge_usd` away from the captured PTB ($2 on 5m, $10 on
   15m/hourly), **and** the book's winning side must match the direction of the
   underlying move. This blocks buying a "winner" that the resolution oracle itself
   disagrees with (stale-book trap).
6. **Risk caps.** `buy_budget` USDC per market ($2.50), `buy_max_spend` hard
   ceiling ($3), `buy_max_shares` sanity rail (default 5; raise it when you
   raise the dollar size), `max_open_positions`, `max_open_notional`,
   `max_daily_notional`, and available USDC balance.

Execution: a FAK **limit** buy sized in **shares** at the quoted ask —
`min(budget/ask, buy_max_shares)` via `OrderArgs` + `create_order` — not a USDC
market order and **not** capped to displayed top size. A dollar-denominated
market FAK still walks cheaper levels (gate quotes 80¢, leftover cash lifts 9¢).
Thin tops log `[THIN ASK]` and still post the dollar size (clipped only by the
share rail); unmatched remainder dies on the FAK. The 75–90¢ **band is
unchanged**; this only pins the limit price to the level that passed the gate.
Re-quote already aborts if the fresh ask left the band. A BUY limit is a
**maximum**, so the exchange can still price-improve; confirmed fills are
**always persisted** (including below-band averages logged as `buy_fill_below_band`
and walks as `buy_fill_walk`). True average is **USDC / shares**, not extra shares
priced at the gate ask. Those fills set `toxic_fill` when the average is below
`toxic_force_exit_below` (default 65¢) **or** filled shares exceed 1.05× quoted
`budget/ask` size, and are **not** ridden to $1: the hedge path force-exits at the next usable
bid (no bounce cancel, no `hedge_min_price` floor; REST may be bid-only). Milder
below-band fills (e.g. 70¢) stay on the normal ≤35¢ hedge path. Delayed FAKs are
polled; zero confirms fall back to balance reconciliation (`buy_ghost_fill`) using
posted USDC as cost when the bag walked.
Before every POST, the bot atomically writes a `buy_uncertain` quarantine with the
exact signed order ID, token, amounts, quoted share size, and pre-submit balance. Any exception, falsy
response, or non-terminal response stops replacement orders and keeps that market
quarantined. Cross-cycle recovery first reconciles that exact order and its trades;
BUY walks are accepted (not `identity_mismatch`). Stable balance observations are only a fallback. Recovery continues after market
expiry, and GC never deletes unresolved quarantine.
Entry cost is USDC spent (`makingAmount` on CLOB v2 BUY), not `shares × gate ask`.

**Skip / fill reasons logged for audit** (visible in logs and research JSONL):

- `buy_skip_ambiguous` — GUI display prices too close to call
- `buy_skip_no_consensus` — ask in band but GUI/book integrity fails (includes
  `up_book_why` / `dn_book_why`: `wide_spread`, `bid_too_low`, …)
- `buy_skip_incomplete_book` — missing GUI price on a leg (no mid, no last trade)
- `buy_skip_underlying_edge` — live oracle < $10 from PTB
- `buy_skip_underlying_side` — book winner disagrees with the underlying move
- `buy_fill_below_band` — fill landed below the ask band; inventory recorded + `toxic_fill`
- `buy_fill_walk` — confirmed BUY shares exceeded the quoted budget/ask size
- `buy_ghost_fill` — balance rose after a null/delayed CLOB confirm

While any position is open or any market is inside the buy window, the loop runs in
"hot mode" (`poll_held_s` / `poll_buy_window_s`, 50–100 ms); otherwise it idles at a
slow poll to save API quota.

---

## 7. The Hedge (Sell-Only Exit)

The bots never take profit. The only sell is the defensive hedge (or a toxic-fill
force exit):

- **Arm (normal):** WS/cache bid ≤ `hedge_threshold` (35¢) while the position is open
  (peek only — never sufficient to sell).
- **Arm (toxic_fill):** entry average `< toxic_force_exit_below` (65¢) arms an
  immediate dump — do not wait for a 35¢ reversal and do not cancel on bounce.
  Below-band but ≥65¢ uses the normal hedge.
- **Confirm (force-fresh REST, fail-closed):** re-fetch the full book with
  `force_rest=True`. Normal hedges skip if either side is missing
  (`hedge_skip_incomplete_rest`) — **no WS fallback**. Toxic dumps may sell when
  REST has a **bid** but no ask; no bid still skips the cycle. If bid bounced
  above threshold on a *normal* entry, abort (`hedge_cancel_bounce`); toxic fills
  skip this cancel.
- **Book integrity (normal only):** a lone penny bid under a still-high ask is **not**
  a reversal (`hedge_skip_toxic_book`). Require bid ≤ 35¢, ask ≤
  `hedge_require_ask_max` (40¢), and spread ≤ `hedge_max_spread` (15¢). Toxic dumps
  skip integrity / `abort_above` so a collapsed book can still exit.
- **Execution:** After the 35/40 check, FAK at the **live bid** (minus undercut).
  There is no “won't sell below 32¢.” 20¢ or 1¢ on a still-tight collapsed book
  is a fill. One tick is only the exchange minimum. Toxic dumps skip integrity /
  bounce cancel and also sell at the live bid. Retries force-REST both sides
  (normal path re-runs the two-sided gate so a spoof 1¢/99¢ still aborts).
- **Outcome:** every POST has a crash-durable deterministic order ID. Only
  settlement-confirmed fills shrink `bought_size` or add proceeds; ambiguous
  outcomes remain in `hedge_uncertain` until exact-order reconciliation. Full
  hedges set `hedge_closed`. Data API balances never invent a sell fill.

---

## 8. Redemption

Winning shares don't sell — they redeem on-chain at $1.00 via Polymarket's relayer:

1. The Data API marks a position leg `redeemable` after resolution.
2. The bot builds `redeemPositions(address,bytes32,bytes32,uint256[])` calldata for
   the CTF contract (`0x4D97…6045`), uses
   `py-builder-relayer-client` to build and sign the PROXY request, verifies the
   derived proxy equals `FUNDER_ADDRESS`, and authenticates `POST /submit` with
   either Relayer API key headers (`RELAYER_API_KEY` + `RELAYER_API_KEY_ADDRESS`)
   or Builder HMAC (`POLY_BUILDER_*` via `py-builder-signing-sdk`).
3. Submissions are throttled per condition (`redeem_throttle_s`, 30 s) and abandoned
   after `max_redeem_age_days` (7 days). Conditions that permanently fail are kept in
   an in-memory blocklist so the bot doesn't burn gas on a reverting call.
4. Submission only sets `redeem_pending`. The bot polls `GET /transaction`; par is
   credited only after `STATE_CONFIRMED` and a complete fresh Data API snapshot
   shows the inventory has disappeared.

The local private key signs the relayer's EIP-712 proxy request; builder/relayer
credentials authenticate the HTTP submission. The key does not directly broadcast
or pay gas for an on-chain transaction.

---

## 9. State, P&L, and Garbage Collection

**State file (`positions_buy*.json`)** — per condition ID: market metadata (question,
end time, token IDs, tick), entry record (`bought_token`, size, cost), hedge
proceeds, `pnl_redeem_value`, `redeem_submitted_at`. This is what lets a restarted
bot resume hedging and redeeming without re-buying. **Never delete or truncate.**

**P&L file (`pnl_buy*.json`)** — append-only record of settled markets:
`entry_cost`, `redeem_value`, `hedge_proceeds`, `net`, `outcome`
(`win` / `hedge` / `loss`). Written with `atomic_save()`.

**Garbage collection:** only terminal evidence (`pnl_redeem_value`, or a confirmed
closed hedge with no remainder) allows an entered market to be folded into P&L and
removed. GC never assumes par. `record_pnl()` is idempotent by condition ID so a
crash between P&L and state saves cannot double-count.

---

## 10. Configuration & Hot Reload

**`.env`** (gitignored, per VM):

| Variable | Purpose |
|---|---|
| `PRIVATE_KEY`, `FUNDER_ADDRESS` | Trading account (key + funder/proxy address) |
| `API_KEY`, `API_SECRET`, `API_PASSPHRASE` | CLOB API credentials (L2 auth) |
| `RELAYER_URL` | Redeem relayer URL (defaults to Polymarket production) |
| `RELAYER_API_KEY`, `RELAYER_API_KEY_ADDRESS` | Relayer API key auth for redeem (preferred; Settings → API Keys) |
| `POLY_BUILDER_API_KEY`, `POLY_BUILDER_SECRET`, `POLY_BUILDER_PASSPHRASE` | Alternate Builder HMAC auth for redeem (Settings → Builders) |

**Strategy JSON** — a valid file is required at startup. Each bot re-reads it every
cycle (`load_strategy()` checks mtime), so ordinary parameter changes take effect on
the next tick with **no restart**. A missing/malformed hot reload disables entries
and retains the last-known-good hedge settings. Unknown keys and invalid types or
ranges are rejected. `dry_run` is startup-only because it selects different state
paths. Templates are `strategy_buy.example.json`,
`strategy_buy5m.example.json`, `strategy_buyhourly.example.json`. Key parameters:

| Key | Default (15m/5m/hr) | Meaning |
|---|---|---|
| `buy_threshold` / `buy_max_price` | 0.75 / 0.90 | Trigger ≥75¢; hard ceiling 90¢ (prefer fills near 75¢) |
| `min_winner_bid` / `max_loser_bid` / `min_bid_edge` | 0.70 / 0.30 / 0.05 | GUI consensus gate (aligned to 75¢ band) |
| `max_entry_spread` | 0.05 | Max ask−bid on winner at entry |
| `underlying_gate_enabled` / `min_underlying_edge_usd` | true / 2.0 (5m), 10.0 (15m/hr) | Oracle alignment gate |
| `toxic_force_exit_below` | 0.65 | Force-dump if FAK avg &lt; 65¢ (must be ≤ buy_threshold) |
| `hedge_enabled` / `hedge_threshold` / `hedge_min_price` | true / 0.35 / unused floor | Arm at 35¢; `hedge_min_price` kept in JSON, not a FAK floor |
| `hedge_max_spread` / `hedge_require_ask_max` | 0.15 / 0.40 | Hedge book must actually collapse |
| `buy_window_min` (15m, hr) / `buy_start_s` (5m) | 4.0 / 120 / 13.0 | Entry window before close |
| `buy_budget` | 2.5 / 2.5 / 2.5 | USDC per market |
| `max_open_positions` / `max_open_notional` / `max_daily_notional` | 0 (=unlimited) / 10k / ~∞ | Risk caps |
| `redeem_throttle_s` / `max_redeem_age_days` | 30 / 7 | Redeem pacing |
| `entry_enabled` | false | Explicit hot-reloadable arm for new entries |
| `dry_run` | true | Startup-only; log `[DRY BUY]`/`[DRY SELL]`, no real orders |
| `poll_buy_window_s` / `poll_held_s` | 0.1 / 0.05 | Hot-loop cadence |
| `tick_size` | 0.01 / 0.001 / 0.01 | Fallback tick size |

**Warning:** `dry_run: false` places real orders with no confirmation prompt.

---

## 11. Operations on the VM

**Services** (unit files in `deploy/`, installed to `/etc/systemd/system/`):

```bash
sudo systemctl status polybuybot polybuybot5m polybuybothourly
# After validating a deploy, start/restart deliberately (CI does not restart):
sudo systemctl restart polybuybot5m
sudo journalctl -u polybuybot -f           # live logs (stdout)
```

Code deploys via GitHub Actions (`.github/workflows/deploy.yml`): pushes to `main`
that touch the buy bots, `buy/`, or `requirements.txt` SSH to the VM, `git pull`,
and `pip install -r requirements.txt`. **Services are not restarted by CI** (a blind
restart would start force-stopped/disabled units). Operators restart after validation.
Strategy JSON changes need no deploy and no restart (hot reload).

**Audit on the VM:**

- `tail -f buybot.log` — structured JSON-line logs (`RotatingFileHandler`).
  Look for `cycle_error`, `buy_skip_*`, `hedge_attempt`/`hedge_fill`,
  `redeem_submit`, `pnl_recorded`.
- `cat .heartbeat_buy5m` — monotonic tick counter; if it stops advancing, the loop
  is wedged.
- `positions_buy*.json` / `pnl_buy*.json` — open entries and settled P&L.
- `underlying_research_buy*.jsonl` / `ptb_*_buy*.json` — oracle/PTB decision audit.
- `python check_path_backtest.py --grid --budget 2.5` — hypothetical entries from
  `pathlog/ticks/` (ask × seconds-left). New ticks include top-of-book **size**;
  the backtest models fillable size (`min(budget/ask, ask_size)`). Live bots
  post the full dollar size at that ask. Legacy ticks
  without size still fill the full budget at the best ask. **Export CSVs / copy
  the ticks dir off the VM regularly** — `pathlog.py` auto-prunes ticks
  (14 days / 400 MB) so they fit the small boot disk. Pruned JSONL is deleted.
- `python check_book.py` — ad-hoc diagnostic: prints book/price data for the current
  hourly market (useful sanity check for book shape).
- Notifications: fire-and-forget ntfy.sh pushes on buys, hedges, redeems, and fatal
  errors (shared topic, e.g. `polybot-joel-btc`). Notification failures must never
  crash the bot.

**Disk:** host logs filled the disk once (2026-07); see `deploy/DISK_OPS.md`. The
journal is capped via `deploy/journald-size.conf`. Pathlog ticks are capped in
`pathlog.py` (14 days / 400 MB); export before prune.

---

## 12. Error Handling Philosophy

- **Never crash the loop.** Every cycle is wrapped; unexpected exceptions are logged
  as `cycle_error` with a traceback and the loop continues. `safe_api_call` filters
  transient API noise (rate limits, 5xx, timeouts) into retries/skips.
- **Fail safe, not fast.** When data is missing or ambiguous (incomplete book, no
  consensus, oracle edge too small), the bot does nothing. Not trading is always the
  default.
- **Persist fills that already happened.** A posted FAK cannot be “rejected” in
  software — below-band buys are logged, written to state with `toxic_fill`, and
  force-exited (not ridden to redemption). Crashes between order and save are
  recovered via Data API positions / balance checks.
- **Complete audit trail.** Every decision — buys, skips, hedges, redeems, P&L — is
  a structured log event and (for buy decisions) a research JSONL row.

---

## 13. Latency & Optional AI Validation

**Will a bigger VM meaningfully improve buy/hedge speed?**
Usually no. The hot path is dominated by network RTT to Polymarket CLOB/Gamma,
REST confirmation, durable `atomic_save` fsync, and SDK round-trips — not local
CPU. Prefer a non-burstable instance in a region with low measured latency to
Polymarket endpoints, and keep hedge work off competing I/O. CPU upgrades alone
do not transform FAK fill latency; some crypto markets may also impose exchange
taker delay that no VM size can remove.

**Can a small open-weight / fast external AI model validate each buy/hedge?**
Not on the synchronous buy/hedge path. Even a fast hosted or local LLM adds
unbounded tail latency and a new failure mode. Deterministic gates (book
integrity, oracle edge, settlement finality, quarantine) remain authoritative.

**Best architecture if AI is desired later:**
1. Keep the current deterministic gates as the only hard blockers for live orders.
2. Run a shadow async scorer (tabular model or tiny classifier) that logs
   agree/disagree with buy decisions without delaying POST.
3. Optionally promote a buy-only veto/downsize after shadow validation; never delay
   or veto hedges — hedge is a safety exit and must stay latency-critical.

---

## 14. Landmines

1. **Three copies, not a library.** A bug fix in `buybot.py` almost certainly applies
   to `buybot5m.py` and `buybothourly.py`. Diff the siblings after any logic change.
2. **5m uses seconds; 15m/hourly use minutes.** The 5m loop keys on
   `seconds_left`/`buy_start_s` (120 s), the others on `minutes_left`/`buy_window_min`.
   Propagating window logic across families without converting units has caused
   production NameErrors.
3. **Slug excludes are load-bearing.** `btc-updown` is a prefix of `btc-updown-5m`;
   the 15m bot must exclude it, and vice versa for hourly. Mis-set excludes → two
   bots buying the same market with separate budgets.
4. **PNL par-redemption fallback.** GC assumes `redeem_value = bought_size` when no
   redeem value was recorded (see §9). Correct for winner-leg buys; wrong if that
   invariant is ever violated.
5. **No module guard.** The scripts run at import; never `import buybot`.
6. **Tick sizes differ.** 5m markets tick at 0.001; 15m/hourly at 0.01.
   `hedge_min_price` must be a valid tick multiple for its market.
7. **State files are sacred.** `positions_buy*.json`, `pnl_buy*.json`, logs,
   heartbeats, PTB stores, research JSONL, `.env`, and live `strategy_buy*.json`
   are gitignored runtime data. Never delete, truncate, or commit them.
8. **Ask ≠ price.** Gate/WS ask can sit at 97¢ over a 1¢ bid. That is not a
   tradable near-certain winner. Entry requires a tight REST book; fills are
   checked against avg USDC/share. Never trust last-trade GUI alone on a wide book.
9. **Bid-alone hedge dumps.** A 1¢ bid under a 99¢ ask is illiquidity, not a
   reversal. Hedge requires ask ≤ `hedge_require_ask_max` and a tight spread, and
   always REST-confirms before selling.

---

## 15. Removed / Historical

This repo previously contained a sell-side bot family (`bot*.py`), an on-chain
atomic-mint buyer (`buy/runner.py` + relayer/contracts modules), a shadow
paper-trading simulator (`sim/`), and their deploy units, configs, and docs. None of
them ran in production anymore and all were deleted in the 2026-08 cleanup; retrieve
them from git history if ever needed. The only supported product is the live buy
path described above.

---

## 16. Glossary

| Term | Meaning |
|---|---|
| **Leg** | One side of a binary market (UP or DOWN token) |
| **Condition ID** | On-chain identifier of a market; key for state and redeem |
| **CLOB** | Polymarket's central limit order book; where FAK orders execute |
| **FAK** | Fill-And-Kill order: immediate execution, no resting remainder |
| **PTB** | Price To Beat — oracle price at window open; resolution compares close vs PTB |
| **Oracle / RTDS** | The data feed that resolves the market (Chainlink TWAP or Binance), streamed over Polymarket's real-time data socket |
| **GUI consensus** | Winner/loser inferred from the Polymarket UI display price (mid or last trade) |
| **Hedge** | Defensive market-sell when the held book actually collapses (bid ≤ 35¢, ask ≤ 40¢, tight spread) |
| **Redeem** | On-chain settlement of a resolved winning leg at $1.00 via the relayer |
| **Hot mode** | Sub-second polling while a position is open or a market is in the buy window |
| **GC** | Garbage collection of resolved/settled market state into the P&L file |
