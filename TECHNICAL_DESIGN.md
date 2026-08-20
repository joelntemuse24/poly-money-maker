# Poly Money Maker — Technical Design

This document is the **guided tour of the live system**. It is written for
someone who can follow Python with some help, not for a compiler.

If a word is jargon, it is defined the first time it appears. If a piece of
code exists only to satisfy Python or the exchange, it is named and skipped.
If a piece of code exists because **real money** or **Polymarket’s weirdness**
forced a design, it is explained until the “why” is obvious.

**Three docs, three jobs:**

| File | Job |
|---|---|
| `CURRENT.md` | What is live **today** (knobs, start/stop). Short. Change it when ops change. |
| `AGENTS.md` | Cheat sheet for coding agents (never-do, file map, how to verify). |
| **This file** | How the program is built, what the important code does, and why. |

Live trading is **only** the 5-minute buy bot (`buybot5m.py` / systemd
`polybuybot5m`) plus a recorder that never places orders (`pathlog.py` /
`polypathlog`). The 15-minute and hourly bots exist as near-copies and are
**stopped**. Minting is **paused**. Do not start those unless the operator
asks.

---

## Table of contents

**Part I — Picture**

1. [What the program is trying to do](#1-what-the-program-is-trying-to-do)
2. [The computer, the wallet, and the files](#2-the-computer-the-wallet-and-the-files)
3. [Map of the repository](#3-map-of-the-repository)

**Part II — Ideas the code assumes**

4. [Binary markets, books, and Fill-And-Kill](#4-binary-markets-books-and-fill-and-kill)
5. [Python shape of the bots](#5-python-shape-of-the-bots)
6. [Polymarket’s five systems](#6-polymarkets-five-systems)
7. [Crash-safe JSON (the “database”)](#7-crash-safe-json-the-database)

**Part III — Walking `buybot5m.py`**

8. [How to read the 5,000-line file](#8-how-to-read-the-5000-line-file)
9. [Startup: lock, secrets, rounding, CLOB client](#9-startup-lock-secrets-rounding-clob-client)
10. [Strategy JSON and hot reload](#10-strategy-json-and-hot-reload)
11. [Logging, notifications, tiny helpers](#11-logging-notifications-tiny-helpers)
12. [Positions: Data API vs local ledger](#12-positions-data-api-vs-local-ledger)
13. [Quotes: websocket vs REST](#13-quotes-websocket-vs-rest)
14. [What “the website shows 80¢” actually means](#14-what-the-website-shows-80-actually-means)
15. [Buy gates and the 5m price bands](#15-buy-gates-and-the-5m-price-bands)
16. [Sizing a buy (Decimal, 2 cents, 4 dp shares)](#16-sizing-a-buy-decimal-2-cents-4-dp-shares)
17. [Posting a buy FAK and living with the result](#17-posting-a-buy-fak-and-living-with-the-result)
18. [The hedge (the only sell)](#18-the-hedge-the-only-sell)
19. [Redemption at $1.00](#19-redemption-at-100)
20. [The main loop, one cycle](#20-the-main-loop-one-cycle)

**Part IV — The rest of the system**

21. [Shared `buy/` modules](#21-shared-buy-modules)
22. [Pathlog (no orders)](#22-pathlog-no-orders)
23. [Tests (the bots cannot be imported)](#23-tests-the-bots-cannot-be-imported)
24. [Operations: systemd, CI, disk](#24-operations-systemd-ci-disk)
25. [Error handling and the `known_cost` stall](#25-error-handling-and-the-known_cost-stall)
26. [Landmines](#26-landmines)
27. [Removed / historical](#27-removed--historical)
28. [Glossary](#28-glossary)

---

## 1. What the program is trying to do

Polymarket lists short Bitcoin **“Up or Down”** markets. Each market is a
yes/no bet with a clock:

> From this start time to this end time (5 minutes, 15 minutes, or 1 hour),
> will BTC finish **up** or **down** versus a starting price?

That starting price is the **Price To Beat (PTB)** — the oracle print nearest
the market’s open. At the end, **exactly one side is worth $1.00** and the
other is worth **$0**. You do not get a partial payout on the token itself.
(You can sell early on the order book, which is a different price.)

A market therefore has two **tokens** (also called **legs**): UP and DOWN.
Buying UP at 80¢ means: “I pay 80¢ now; if UP wins I redeem $1.00; if DOWN
wins I get nothing unless I sold first.”

**This bot does not make a market.** It does not leave orders sitting on the
book hoping someone hits them. It waits until the book already looks decided,
**buys the favorite**, and holds. If the favorite later looks **actually
dead**, it **sells** (the **hedge**) to bound the loss. If the favorite wins,
it **redeems** on-chain for $1.00 per share. There is **no** “take profit at
+$0.30” sell. That idea was considered and dropped: on a 90¢ fill you cannot
even make $0.30 before $1.00, and on an 80¢ fill selling at 90¢ throws away
the rest of the ride to $1.00.

**Live 5m entries (union of windows):**

| Time left until close (TTM) | Winning ask the bot will consider |
|---|---|
| More than 300 seconds | none — too early |
| 120s < TTM ≤ 300s (first ~3 minutes) | **90¢ through 99¢** |
| 60s ≤ TTM ≤ 300s (first ~4 minutes) | **also 95¢ through 99¢** |
| TTM ≤ 120s (last 2 minutes) | **75¢ through 99¢** |

Last 120s is the full 75–99¢ range. There is **no** 91–94¢ hole. The ≥95
window still overlaps TTM 60–120s, but late already covers those asks.
First 3 minutes stay **90–99¢** (not 75¢). Do not raise live `buy_max_price`
to 0.99 — that knob is the early ≥90 floor; last-120s cap is
`early_buy_max_price`.

**Budget:** $2.50 per market, hard cap $3, share rail 5 shares. At 75¢ that is
about 3.3 shares. If that ride wins, profit is roughly
`(1.00 − 0.75) × 3.3 ≈ $0.83` before fees. A 95¢ fill that wins is only about
5¢ per share.

**Hedge (5m):** sell only when the **held** book looks collapsed — bid ≤ 50¢
**and** ask ≤ 55¢, spread tight — **and** the website-style prices agree that
side actually lost. Then sell at whatever the live bid is (even 20¢). The
15m/hourly copies still use 35¢/40¢; they are not running.

Everything else in this document exists to do **that** without:

- buying a fake 97¢ ask that sits over a 1¢ bid,
- double-spending after a crashed POST,
- selling a still-winning position because one spoof bid printed 1¢,
- filling the 10GB VM disk,
- or running two copies of the bot at once.

---

## 2. The computer, the wallet, and the files

**Machine:** a Google Compute Engine VM named `instance-20260516-185922`.
Linux user `ntemusejoel`. Working directory `~/poly-money-maker`. Python
virtualenv `.venv`. Boot disk is small (~10GB). That last sentence is why
logs and pathlog ticks are capped.

**systemd** starts processes on boot and restarts them if they die:

| Unit | Script | Live? |
|---|---|---|
| `polybuybot5m` | `buybot5m.py` | **yes — places orders** |
| `polypathlog` | `pathlog.py` | **yes — GET books only** |
| `polybuybot` | `buybot.py` | stopped |
| `polybuybothourly` | `buybothourly.py` | stopped |
| `polymintbot` | `mintbot.py` | paused |

Unit files live in `deploy/` and are copied to `/etc/systemd/system/`. The 5m
unit loads `.env` (secrets). Pathlog’s unit does **not** — it needs no keys.

**Wallet:** Polymarket uses a **proxy** address (`FUNDER_ADDRESS`) that holds
the USDC and the outcome tokens. `PRIVATE_KEY` is the signer. CLOB API keys
(`API_KEY` / `API_SECRET` / `API_PASSPHRASE`) authenticate HTTP to the order
book. Relayer/Builder keys authenticate **redemption** HTTP. None of these
belong in git. Cloud research agents never get `.env`.

**Live JSON on disk (gitignored, never delete):**

| File | Role |
|---|---|
| `strategy_buy5m.json` | Knobs. Re-read every loop. This is what the running bot uses, not the `.example` file. |
| `positions_buy5m.json` | Open markets, fills, quarantines, redeem pending |
| `pnl_buy5m.json` | Settled P&L rows |
| `buybot5m.log` | One JSON object per line (rotated) |
| `.heartbeat_buy5m` | Unix time; if it stops moving, the loop is stuck |
| `ptb_twap30_buy5m.json` | Cached Price To Beat per market |
| `underlying_research_buy5m.jsonl` | Buy/skip audit for later analysis |
| `pathlog/ticks/*.jsonl` | Recorded books (auto-deleted after 14 days or 400 MB) |

Templates you **may** commit: `strategy_buy5m.example.json` (and 15m/hourly
twins). The bot **never** loads `.example`.

`dry_run: true` points state/log/PTB at `*.dryrun.*` names so a paper process
cannot smash live files. `dry_run` is chosen at **startup** because those
paths are picked once. Other knobs hot-reload.

---

## 3. Map of the repository

```
buybot5m.py          LIVE 5m bot (~5177 lines). You are usually here.
buybot.py            15m copy (~5063). Stopped.
buybothourly.py      Hourly copy (~5061). Stopped.
buy/                 Importable helpers (safe — they do not start trading)
  market.py          Find markets on Gamma
  btc_price.py       Chainlink / Binance websocket + PTB
  clob_book_ws.py    Fast top-of-book websocket
  book.py            Parse bid/ask levels (price + size)
  entry_skip.py      5m band math + skip labels (5m + tests only)
  chain.py           Mint helper: Polygon eth_call (paused path)
  contracts.py       Mint helper: splitPosition calldata (paused path)
pathlog.py           Recorder, no orders
check_*.py           Offline / log diagnostics
tests/               unittest (CI)
deploy/              systemd units + disk notes
widget/polydesk.py   Laptop glance (public API, no orders)
```

**Rule of copies:** a buy/hedge/quarantine bug in `buybot5m.py` probably
exists in the other two files. Diff them after a logic change. The 5m-only
exceptions are the early price bands (`buy/entry_skip.py` + `BUY_HORIZON_S`)
and 5m defaults (hedge 50/55, BTC gate $0, tick `0.001`, windows in
**seconds**).

15m/hourly windows are in **minutes** (`buy_window_min`). Mixing the two
without converting units has caused production `NameError`s. The 5m loop
**must** compute `seconds_left`. If someone ports a 15m snippet that only
defines `minutes_left`, the 5m process dies every cycle.

---

## 4. Binary markets, books, and Fill-And-Kill

**Token price** is a probability on (0, 1). 0.80 means 80¢. 5m markets tick
in tenths of a cent (`0.001`). 15m/hourly tick in whole cents (`0.01`).

**Order book:** people willing to **buy** the token (bids) and people willing
to **sell** it (asks). The **best bid** is the highest price a buyer will
pay. The **best ask** is the lowest price a seller will take. The bot **buys
at the ask** (it is taking someone else’s sell) and **hedge-sells at the
bid** (it is hitting someone else’s buy).

**Spread** is `ask − bid`. A 97¢ ask over a 1¢ bid is a **96¢ spread**. That
is not “the market is 97¢.” It is an empty book with a decorative ask. Last
trade on the website can still look like a winner. The bot therefore requires
a **tight** book at entry (spread ≤ 5¢ and bid ≥ 70¢).

**Fill-And-Kill (FAK):** “take whatever is available at my limit **right
now**, cancel the rest.” Nothing rests. If the ask was 1 share and we wanted
3, we get 1 (or nothing). We do **not** leave 2 shares sitting. That is
deliberate: a resting order can fill later at a junk price after we walked
away.

**Why a limit buy, not a “market buy in dollars”:** a dollar market FAK spends
leftover USDC down the book. Quote 80¢, leftover cash lifts a 9¢ ask, you own
junk. The 5m bot sizes **shares** = `budget / ask` and posts a **limit at the
open band max (99¢)** so the FAK can take 84–99¢ if the 83¢ clip is gone.
Unfilled dollars still die. 15m/hourly (stopped) still limit at the touch.

**Displayed size is not a cap.** If the top ask shows 0.4 shares, the bot
still posts ~3 shares at the **99¢ limit**. The exchange fills what exists
between the touch and that cap. Thin books log `[THIN ASK]`.

---

## 5. Python shape of the bots

### One file, one process, one `while True`

There is no web server, no database, no `asyncio`. The 5m bot is a script
that:

1. does a lot of setup at **import time** (the moment Python starts the file),
2. then sits in `while not _shutdown_requested:` forever.

**There is no `if __name__ == "__main__"`.** In many Python tutorials that
guard means “only run when I type `python buybot5m.py`, not when I
`import buybot5m`.” These bots skip it on purpose: the file **is** the
program. Consequence: **`import buybot5m` would start trading.** Tests must
not import the bot. They import `buy/` and `check_*.py`, or they **cut
function source out of the file with the `ast` module**
(`tests/test_buy_fill_shapes.py`).

### Why not a shared library for the three bots?

Historically the three cadences drifted (seconds vs minutes, tick size,
oracle). A shared 5,000-line module that you `import` is how you accidentally
start a bot. Shared pieces that are **safe** to import were moved to `buy/`.
The rest stays copied. That is ugly and it is the design: **diff the
siblings**, do not invent a framework on a live money path.

### Threads, but the brain is still the loop

A **thread** is a second stack of work inside the same process. Used here
for I/O that should not freeze the loop:

- CLOB book **websocket** (`buy/clob_book_ws.py`) — daemon thread, cache in
  memory.
- BTC **RTDS** websocket (`buy/btc_price.py`) — same idea.
- `ThreadPoolExecutor` pools in the main file: extra REST book fetches,
  Gamma/positions/balance refresh, ntfy, redeem status.

The **decision** (buy or not, sell or not) still happens on the main loop.
Threads fill caches. A stale cache is treated as “no data” (fail closed), not
as a price.

`daemon=True` on the websocket threads means: when the main process exits,
those threads die with it. They are not meant to outlive the bot.

### Process lock (`fcntl.flock`)

Near the top of `buybot5m.py`:

```python
def acquire_process_lock(path):
    lock_fh = open(path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        ...
        raise SystemExit(1)
```

`fcntl.flock` is a **kernel lock** on a file. `LOCK_EX` = exclusive (only one
holder). `LOCK_NB` = do not wait; if someone else has it, fail immediately.
The path is `/tmp/poly-money-maker-buybot5m.lock`. If you `python buybot5m.py`
while systemd already runs it, the second process **exits**. Two live
instances would both think they may spend $2.50.

The function **returns the open file handle** and the module stores it in
`_PROCESS_LOCK_FH`. If that handle were closed, the lock would release. Do
not “clean up” that global.

Pathlog and mint use the same idea with repo-local lock files.

### Ctrl-C does not yank the rug

`signal.signal(SIGINT, _handle_shutdown)` (and `SIGTERM` for systemd)
sets `_shutdown_requested = True`. The loop finishes the **current cycle**
then exits. That avoids killing the process between “POST the order” and
“save the order id.”

---

## 6. Polymarket’s five systems

Mixing these up is how you sell on a fake 1¢ bid.

| Name | What it is | What we use it for |
|---|---|---|
| **Gamma** | Catalog HTTP API | List markets: slug, condition id, token ids, end time, later `winner` |
| **CLOB REST** | Order book HTTP | `/book` (authoritative quote before an order), `/order` POST, balances, last trade |
| **CLOB websocket** | Push top-of-book | Speed. **Arms** a check. Never enough to POST a normal buy/hedge by itself |
| **Data API** | Account HTTP | Positions, redeemable flag, trades. Can **lag** after a fill |
| **RTDS** | Live data websocket | The **same** BTC series the market resolves on (TWAP 30s for 5m) |
| **Relayer** | Polymarket submits Polygon txs | `redeemPositions`. We sign a proxy request; we do not broadcast our own gas |

**Condition ID:** 32-byte hex id of the market on-chain. Dictionary key in
`positions_buy5m.json`. Argument to redeem.

**Token ID:** CLOB asset id for UP or DOWN. What you actually buy/sell.

**CLOB v2 amounts:** on a BUY, `makingAmount` is USDC you paid,
`takingAmount` is shares you received. Cost is USDC spent, **not**
`shares × the gate ask`. Delayed stubs sometimes omit `makingAmount`; the
code then estimates and still records inventory if shares appeared.

**HTTP 400 `"no orders found to match"`:** the FAK crossed an empty book.
That is “nothing there,” not “auth failed.” Safe to **re-quote** and try
again in the same trigger (up to 3). Other 400s (`invalid amounts`, auth)
must **not** retry a second $2.50.

**HTTP 400 `invalid amounts`:** USDC must be **2 decimal places** (cents).
Taker shares **4 decimals**. The SDK also round-downs some sizes to 2 dp, so
naive 4 dp share math signs as `$2.4999` and the CLOB rejects it.
`quoted_buy_shares` exists for that. `check_buy_rejects.py` counts those
in logs — they passed every strategy gate and still did not post.

**Chain:** Polygon, `CHAIN_ID = 137`. Collateral token (pUSD) and Conditional
Token Framework (CTF) addresses are constants at the top of the bot. You
almost never change them.

---

## 7. Crash-safe JSON (the “database”)

`atomic_save(path, data)` is the write path for state and P&L.

1. Write `path.tmp`.
2. `flush()` then `os.fsync(file)` — force bytes onto disk, not just the OS
   cache. A power cut after `write()` without `fsync` can leave a half file.
3. `allow_nan=False` on `json.dump` — JSON has no `Infinity`. A NaN in state
   would round-trip as garbage. `load_json` also rejects non-finite numbers
   via `parse_float` / `parse_constant`.
4. If the current `path` is valid JSON, copy it to `path.bak` (also fsynced)
   **before** replacing. After a crash you still have one good file.
5. `os.replace(tmp, path)` — atomic rename on the same filesystem.
6. `fsync` the **directory**. On Linux, a rename is not durable until the
   directory entry is flushed.

`load_json` tries `path` then `path.bak`. Live mode (`dry_run` false) calls
`load_json(STATE_FILE, required=True)`: missing state **refuses to arm**
rather than inventing empty positions and maybe buying a market you already
own.

**Write-ahead quarantine:** before a live POST, the bot saves
`buy_uncertain_*` (or hedge twins) including the **deterministic signed
order id**, token, quoted shares, spend, and pre-submit token balance. If the
process dies during the HTTP round-trip, the next cycle **inspects that exact
order** instead of posting a second $2.50. This is the most important
money-safety pattern in the file.

---

## 8. How to read the 5,000-line file

`buybot5m.py` is one scroll with **comment banners** as chapters:

| Banner | What lives there |
|---|---|
| (top) | Imports, `ImmediateResponseClobClient`, process lock, URLs, `.env` |
| `STRATEGY CONFIG` | Defaults + `load_strategy()` |
| `LOG ROTATION` | JSON-line log + ntfy |
| `HELPERS` | `safe_api_call`, `finite_float`, `atomic_save` |
| `P&L` | `record_pnl` |
| `POSITIONS` | Data API fetch, merge with local ledger |
| `PRICING` | Books, GUI, hedge/buy gates, share math, fees |
| `TICK SIZE` | Cache of CLOB tick |
| `ORDER HELPERS` | Decode fills, confirm size, quarantine inspect |
| `BUY` | `buy_market_with_retry` |
| `SELL (for hedge only)` | `sell_market_with_retry` |
| `REDEEM` | Relayer submit + poll |
| `CLIENT SETUP` | Construct `ClobClient` — **this runs at import** |
| `SHUTDOWN` | SIGINT/SIGTERM |
| `MAIN LOOP` | `while True`: reload, refresh, redeem, per-market hedge/buy, sleep |

Read the banners, then the function **docstring**, then the fail-closed
branches (`return` / `continue` / `break`). The happy path is usually the
last branch.

15m/hourly files are the same shape with different constants.

---

## 9. Startup: lock, secrets, rounding, CLOB client

**Order at import (this is the real “main”):**

1. `load_dotenv()` — fill `os.environ` from `.env`.
2. Process lock (above). If this exits, nothing else runs.
3. Read env vars into module globals. Missing keys stay `None` until a later
   check.
4. Patch `ROUNDING_CONFIG`: CLOB rejects taker amounts with more than **four**
   decimal places. The SDK’s rounding table is clamped to 4.
5. `load_strategy()` **must succeed**. No valid `strategy_buy5m.json` →
   process dies. Better a crash at start than a bot with invented knobs.
6. If `dry_run`, rename state/log/PTB to `*.dryrun.*`.
7. Build `ImmediateResponseClobClient`.

### `ImmediateResponseClobClient`

The official `ClobClient` can sit for up to ~30s after POST waiting for trade
hashes. Matching already happened on the server. This subclass overrides
`_resolve_transactions_hashes` to **return the POST body immediately**. The
bot already saved the order id and does its **own** settlement checks. Waiting
inside the SDK would delay **hedges on other markets** in the same process.

`retry_on_error=False` on the client: the SDK must not silently retry a POST.
Retries are explicit in `buy_market_with_retry` / `sell_market_with_retry`.

`signature_type=1` and `funder=FUNDER_ADDRESS` mean: sign as the user, settle
against the **proxy** wallet (the Polymarket account people see).

API creds: if `API_KEY` trio is set, use them; else derive from the private
key (prints `AUTH derived`). Then `update_balance_allowance` so USDC.e is
approved for the exchange — a one-time-style sync; failure is a warning, not a
hard exit (you will fail later on POST if allowance is actually missing).

Then: `MarketGateway`, `get_btc_feed(...)`, `get_book_feed()`. Those start
background threads. Then the ASCII banner (the 97¢ / 65¢ text in the banner
is **cosmetic leftover** — live knobs are 75–99 / 50/55). Then
`load_json(STATE_FILE)` and the `while` loop.

---

## 10. Strategy JSON and hot reload

`_STRATEGY_DEFAULTS` is a Python dict of every legal key and its type. That
dict **is the schema**.

`load_strategy()`:

- If the file is **gone after a successful start**: do not crash the process
  (that would strand a live position with no hedge). Disable **entries**
  (`entry_enabled=False`) and **force `hedge_enabled=True`**. Held inventory
  must still be able to exit.
- If mtime unchanged: return cached dict (cheap).
- Else: start from defaults, overlay JSON. Unknown keys → error. Booleans
  must be real JSON `true`/`false`, not `"true"` strings. Numbers are cast
  with `type(default)(value)` so `1` can become `1.0`.
- Legacy: if JSON has `shares` but not `buy_budget`, treat `shares` as the
  dollar budget.
- Then a wall of validators: bands nest correctly, `hedge_require_ask_max >=
  hedge_threshold`, tick is a CLOB tick and equals `EXPECTED_TICK_SIZE`
  (`"0.001"` on 5m), `one_entry_per_market` stays true, live mode cannot
  disable hedge, `buy_max_shares` is large enough for `budget/threshold`,
  `max_open_positions == 0` means unlimited, no NaNs.

**Hot reload vs restart:**

| Change | Needs restart? |
|---|---|
| Most knobs in live JSON | No — next loop iteration |
| `dry_run` | **Yes** (state paths) |
| Python code | **Yes** (`systemctl restart polybuybot5m`). `git pull` is not a restart. |
| Hedge 50/55 if live JSON still says 0.35/0.40 | The **file** wins. Code defaults only apply when the key is **omitted**. Patch the JSON. |

`entry_enabled` is the operator arm. Defaults `false` in the example file.
Live JSON on the VM is what actually arms buys.

---

## 11. Logging, notifications, tiny helpers

`log_event("buy_skip", reason="ask_below_band", ...)` writes one JSON object
to `buybot5m.log` via `RotatingFileHandler` (5 MB × backups). Agents grep
these names. `check_buy_skips.py` counts them. **Throttled** skips
(`log_buy_skip_throttled`) fire at most about once per 8 seconds per
market+reason — they are **not** “one line per missed market.”

`notify(...)` pushes ntfy.sh (`polybot-joel-btc`) on a thread. Failures are
swallowed. A phone notification must never kill the bot.

`safe_api_call(func, ...)` runs `func` and turns 429 / 5xx / timeout-shaped
errors into retries/skips instead of crashing the cycle.

`finite_float(value, minimum=0, maximum=1)` is the standard “is this a real
number in range?” helper. `None`, `""`, `NaN`, and out-of-range become
`None`. Almost every price goes through this. Do not use raw `float()` on
exchange JSON without it.

---

## 12. Positions: Data API vs local ledger

`fetch_all_position_rows` pages the Data API. `build_held_positions` keeps
rows whose slug starts with `SLUG_PREFIX` (`btc-updown-5m`) and **not**
`SLUG_EXCLUDES`. Outcome `yes` maps to UP, `no` to DOWN (Polymarket naming
drift).

**The Data API lags.** You can buy, POST returns, local JSON has 3.3 shares,
and Data API still shows 0 for a while. If the hedge loop trusted Data API
alone, it would **skip hedging** a position that exists, or **re-buy** a
market it already owns.

`merge_tracked_positions(api_held, tracked_meta)` therefore **inserts local
confirmed inventory** into the in-memory `held` map when Data API omitted it.
Local `bought_size` after a confirmed **sell** is also a ceiling: Data API
must not resurrect a larger bag.

`add_tracked_market_stubs` keeps expired-but-unsettled markets in the loop
so redeem/GC still run after Gamma drops them from “active.”

Risk caps (`max_open_positions`) count **non-redeemable** size only.
Redeemable leftovers are settlement backlog. If they counted as “open,” a
slow redeem would freeze all new buys (silent `continue`). Probe uses
`max_open_positions = 0` meaning unlimited.

---

## 13. Quotes: websocket vs REST

**Websocket** (`book_ws.quote(token_id)`): fast, can be stale or
price-only (size zeroed when price moved). Used to **notice** “bid just
printed 48¢” so the loop bothers to check a hedge. `quote_age` older than
`hedge_quote_max_age_s` (0.25s) is not “fresh.”

**REST** (`get_book_quote` → CLOB `/book`): slower, full book, parsed by
`buy/book.py` `best_from_levels`. Used when about to **spend**.

`get_quote_fast(..., force_rest=True)` **bypasses WS and the 200ms REST
cache**. Buy retries and hedge confirms always force REST. A stale snapshot
at POST time is how you buy 80¢ that is already 99¢ or sell into a recovered
book.

WS cache details that matter:

- A **price-only** event that moves the top price **zeros displayed size**.
  Callers must REST before trusting size.
- Out-of-order server timestamps are dropped.
- Tokens not in the current `set_tokens(...)` watch list are forgotten
  (markets roll every 5 minutes).

`get_book_bid` / `_book_cache` are convenience wrappers around the same
quote tuple: `(bid, bid_size, ask, ask_size, mid)`.

---

## 14. What “the website shows 80¢” actually means

Polymarket’s UI does **not** always show last trade. Documented rule,
implemented as `polymarket_display_price`:

- If bid and ask exist and `ask − bid ≤ 10¢`, show the **midpoint**.
- Otherwise show **last trade**.

`get_last_trade_price` hits CLOB `/last-trade-price` with a 0.25s cache, then
falls back to a timestamped field on the last `/book` snapshot if that
snapshot is young enough.

**Buy GUI consensus:** winner display ≥ 70¢, loser ≤ 30¢, gap ≥ 5¢. Both
legs need a display price (mid or last trade) or the bot logs
`buy_skip_incomplete_book`.

**Hedge GUI** is the **inverse** on the held token: held last trade ≤ ask-max
(55¢ on 5m), held GUI ≤ 30¢, other GUI ≥ 70¢. A 48¢/52¢ book with last trade
**85¢** is a clip, not a reversal → `hedge_skip_no_consensus`.

`entry_book_ok`: both sides present, not crossed, spread ≤ 5¢, bid ≥ 70¢.

`hedge_book_ok`: bid ≤ threshold (50¢), ask ≤ 55¢, spread ≤ 15¢. A 1¢ bid
under a 99¢ ask fails (`hedge_skip_toxic_book`).

`toxic_dump_book_ok`: **only** `bid ≤ hedge_threshold`. Used when a fill was
already classified junk (`toxic_fill`). Still will not dump a recovered 97¢
bid.

---

## 15. Buy gates and the 5m price bands

`buy/entry_skip.py` is pure functions (no network). Tests import it directly.

`EntryBand` is a small named tuple: min price, max price, whether the min is
exclusive, and a name (`late` / `early` / `early_95`).

`applicable_entry_bands(seconds_left, ...)` appends every band whose TTM
window contains `seconds_left`. **Union, not first-match-wins.** Last 90s
can be late 75–99 **and** early_95 ≥95 at the same time.

`ask_in_any_band` is the gate.

`select_entry_band` picks the matching band with the **lowest retry floor**
so FAK retries are pinned to the **widest** legal range. A 96¢ ask in the
last 120s matches **late** (floor 75¢), not ≥95 — a walk to 91¢ stays in-band.

`union_ask_band_reason` labels skips: below the lowest floor →
`ask_below_band`; above the highest cap → `ask_above_band`. Live last-120s
75–99 has no hole; `ask_out_of_band` is for prices between disjoint bands
if those return.

`buybot5m.current_entry_bands` is a one-liner wrapper. Last-120s
`late_max` is `EARLY_BUY_MAX_PRICE` (0.99), **not** `BUY_MAX_PRICE` (0.90).
`BUY_MAX_PRICE` remains the first-3-min ≥90 floor. `BUY_HORIZON_S =
max(120, 300, 300) = 300` so hot polling and websocket subscribe actually
run in the first 3–4 minutes. If horizon were still 120, the early bands
would exist in JSON and never be looked at.

**Other gates (all must pass), in the loop after “we do not already hold”:**

1. `ENTRY_ENABLED`
2. Fresh Gamma discovery (stale catalog → hedge-only)
3. Fresh positions snapshot and fresh USDC balance (never spend against
   unknown collateral)
4. Not `buy_uncertain` (quarantine)
5. Not `bought_token` (one entry)
6. Past `buy_grace_s` after first sighting
7. Risk caps
8. Cooldown after empty FAK
9. GUI consensus + tight REST book
10. Ask in an open band
11. Underlying: live TWAP vs PTB, side matches, 5m `$0` means any **non-zero**
    tick (flat still skip). Missing PTB → skip (`buy_skip_underlying_edge`).

The underlying feed is **Chainlink BTC/USD TWAP 30s** for 5m, because that is
what the market **resolves** on. Using Binance here would buy a CLOB winner
the oracle might disagree with.

---

## 16. Sizing a buy (Decimal, 2 cents, 4 dp shares)

`quoted_buy_shares(budget, ask, share_cap)` sizes the share count.

`quoted_buy_shares_up_to_limit` (5m only) starts from that count, then
shrinks until `size × band_max` is exact cents and ≤ `buy_max_spend`.

1. Quantize budget down to **cents** (`Decimal("0.01")`, `ROUND_DOWN`).
2. `shares = spend / ask`, quantized to **0.01 shares** (2 dp), round down.
3. Clip to `buy_max_shares` (default 5).
4. **Loop:** while `shares * ask` is not an exact cent, subtract 0.01 shares.
   The CLOB + SDK will reject `$2.4999`.
5. If it cannot find a legal pair, return `0.0` (no POST).

`Decimal(str(x))` not `Decimal(x)`: `float(2.5)` is already binary-fuzzy;
going through the string of the intended decimal is the usual money pattern.

`buy_fill_walked`: confirmed shares > 1.05 × quoted. That means the bag
walked cheaper levels. `classify_buy_fill` then sets `toxic_fill` if average
`< toxic_force_exit_below` (65¢) **or** walked. Average is
`USDC / shares` (`implied_buy_average`), never “extra shares × gate ask.”

Fees: `_fill_fee_usdc` is the CLOB v2 taker curve
`shares * rate * (p*(1-p))**exponent`. Small at 80–90¢. Net cost/proceeds
subtract it when the schedule is known.

---

## 17. Posting a buy FAK and living with the result

`buy_market_with_retry(...)` is the only buy POST path.

**Dry run:** print `[DRY BUY]`, log `dry_buy`, return zeros, status `"dry"`.
No client call.

**Live:**

1. Read token balance **baseline**. If unreadable → abort (cannot later tell
   a ghost fill from nothing).
2. Up to 3 attempts:
   - Force REST quote. No ask / ask left the band / book went wide → stop.
   - Size shares. Thin displayed size → log, still post full dollar size.
   - Optional `pre_submit` hook (loop uses this for last-second gates).
   - **`on_submit` write-ahead** (quarantine JSON) **before** POST. If this
     save fails → **do not POST** (`persist_fail`).
   - Sign order; `signed_order_id` hashes EIP-712 typed data so the id is
     **determined before** the network — crash recovery can look it up.
   - POST FAK limit at the **band max** (5m: 99¢). Size is still `budget/ask`.
3. `confirm_fill_size` decides matched shares:
   - Terminal `matched` + confirmed trades → trust making/taking.
   - `delayed` POST can echo the **unsigned full size** before any match —
     **do not** treat that as a fill. Poll `GET order` `size_matched`.
   - Brief `GET order` 404 after a live match is normal; poll, do not call
     it empty yet.
4. Proven empty **400 no orders found to match**: if balance **rose** anyway
   → `buy_ghost_fill` `via=unmatched_400_guard`, **stop** (no second FAK).
   If balance unreadable → `buy_attempt_ambiguous`, quarantine, **stop**.
   If truly empty → retry with a fresh quote.
5. Other 400s → no retry.
6. Exception / unclear POST → reconcile **once**, then **stop**. Retrying
   the full budget after an accepted-but-timeout POST is how you double
   spend.
7. `on_fill` durable-saves after every confirmed increment. If save raises,
   no further attempts.

The main loop then writes `bought_token`, `bought_size`, `pnl_entry_cost`,
maybe `toxic_fill`, and will not buy that condition again.

**Unmatched empty cooldown:** after a fully empty trigger, wait
`empty_fak_cooldown_s` (0.15s) before the outer loop tries that market
again. Inner retries are immediate.

---

## 18. The hedge (the only sell)

`sell_market_with_retry` docstring says hedge-only. There is no profit-take
caller.

**Normal hedge pipeline in the loop:**

1. **Peek WS.** If a **fresh** WS bid is **above** 50¢, skip REST entirely
   (`hedge_cancel` / toxic recovered). Do not hammer `/book` on a healthy
   winner.
2. Else **force REST**. Missing a side on a **normal** hedge →
   `hedge_skip_incomplete_rest` (no WS sell). Toxic dump may proceed with
   bid-only; no bid still skips.
3. Bid bounced above 50¢ on REST → `hedge_cancel_bounce`.
4. `hedge_book_ok` 50/55/15. Fail → `hedge_skip_toxic_book`.
5. `hedge_consensus_ok` inverted GUI. Fail → `hedge_skip_no_consensus`.
6. Write-ahead hedge quarantine, then FAK **sell at live bid minus
   `hedge_undercut_ticks`** (default 2 ticks = 0.002 on 5m).
   `hedge_sell_price` **ignores** `hedge_min_price` so a leftover 32¢ config
   cannot refuse a 20¢ print. Floor is one tick (exchange minimum).
7. Retries force REST again and re-run two-sided integrity so a spoof
   1¢/99¢ still aborts. Toxic retries set `abort_above=None` but still
   honor “bid recovered.”

**`toxic_fill`:** armed from junk/walk average. Stays on `meta` forever until
the dump actually sells (or you ride a recovered book). Dump **only while
bid ≤ 50¢**. Recovered 97¢ → `hedge_skip_toxic_recovered`, flag stays armed.

**Reconcile sells** with `reconcile_hedge_sold`: CLOB-confirmed sold size
wins; a single low Data API read must not invent extra fills or erase
confirms. `stable_zero_balances` requires **repeated** successful zeros
before treating the bag as gone.

Partial hedge: `bought_size` shrinks, `hedge_closed` only when remainder
< 0.01. Ambiguous sell → `hedge_uncertain_*`, same inspect-exact-order
pattern as buys.

---

## 19. Redemption at $1.00

Winning shares are **not** sold at 99¢. After resolution, Data API sets
`redeemable`. The bot:

1. Skips redeem HTTP if a hedge is active or a buy window is open —
   redeem latency must not delay a 50¢ dump.
2. Skips if `buy_uncertain` / `hedge_uncertain` (settle execution first).
3. Throttles per condition (`redeem_throttle_s` 30s).
4. Builds `redeemPositions` calldata for the CTF.
5. `submit_proxy_tx`: fetch relayer nonce, encode proxy call, EIP-712 sign
   with `PRIVATE_KEY`, check derived proxy == `FUNDER_ADDRESS`, POST `/submit`
   with Relayer API key headers **or** Builder HMAC.
6. Store `redeem_pending` + `redeem_tx_id`. **This is not P&L.**
7. Background `GET /transaction`. Credit par only when relayer says
   confirmed **and** a complete Data API snapshot shows inventory gone.
8. Permanent revert → in-memory blocklist (do not burn gas every cycle).
9. Age out after `max_redeem_age_days` (7).

GC (`gc_can_finalize`): only with terminal evidence (redeem value recorded,
or hedge closed with dust remainder). `record_pnl` is idempotent by
condition id. Fallback `redeem_value = bought_size` assumes winner-leg par —
wrong if that invariant is ever broken (landmine).

---

## 20. The main loop, one cycle

After import, `while not _shutdown_requested:`:

1. Increment `CYCLE`, snapshot clocks.
2. Filter cached Gamma markets: active, not closed, not `neg_risk`, end in
   the future.
3. `load_strategy()` and copy keys into the uppercase globals the rest of
   the file uses (`HEDGE_THRESHOLD = _strat["hedge_threshold"]`, etc.).
   Recompute `BUY_HORIZON_S`.
4. Kick/collect thread-pool refresh of positions, USDC balance, Gamma
   discovery (staggered by `positions_refresh_s` / `balance_refresh_s`).
5. Heartbeat file.
6. Rich table every `ui_every_n_cycles` in hot mode (cosmetic).
7. Redeem phase (skipped if hedging or entries are live).
8. GC / redeem-status poll.
9. **Sort markets held-first** so a reversal is checked before a new buy
   in the same cycle.
10. `for m in markets:` **try/except per market**. Fault logs
    `condition_id` and **continues**. This is what stopped a single
    `NameError` from skipping every later hedge (see §25).
11. Inside the try: seconds_left; merge held sizes; resolve `buy_uncertain`
    / `hedge_uncertain` via exact order id; **hedge check** if held;
    **buy check** if not held and bands open.
12. Outer `except` → `cycle_error` (refresh/GC/UI/redeem failed). Process
    stays up. Banner does not sleep 5s.
13. Sleep: 0.05s if holding, 0.1s if something is inside `BUY_HORIZON_S`,
    else 1s.
14. Submit background REST quotes for tokens in horizon or held; 
    `book_ws.set_tokens(watch_set)` (horizon + 30s slack).

That is the whole runtime. There is no second scheduler.

---

## 21. Shared `buy/` modules

**`buy/market.py` — `MintMarket` and `MarketGateway`**

`MintMarket` is a frozen dataclass: condition id, slug, question, start/end,
up/down token ids, flags (`active`, `closed`, `neg_risk`). The name
“MintMarket” is historical (mint bot used it first). Buy bots use the same
object.

Start time: slugs look like `btc-updown-5m-1786528500`. The trailing unix
time is the **real** open. Gamma’s `startDate` is sometimes event creation,
hours off. Wrong start → wrong PTB window → wrong BTC gate. The parser
rejects metadata whose duration is not ~5/15/60 minutes.

`MarketGateway.discover([series_slug])` hits Gamma, caches a few seconds,
returns a list of `MintMarket`. `discovery_fresh` is a bool the buy loop
requires before new entries.

**`buy/btc_price.py`**

One `BtcUnderlyingFeed` per source. Daemon thread on
`wss://ws-live-data.polymarket.com`. Ring buffer ~12,000 samples (~3 hours
at 1 Hz). `live_price()` returns `None` if older than 5s (stale). PTB:
nearest tick to `start_ts` within 2s skew, persisted to `ptb_*_buy*.json`.
Missed the open → no PTB → no buy.

`append_research` appends one JSON line and rotates at 50 MiB so a skip
storm cannot fill the disk.

**`buy/book.py`**

`best_from_levels(levels, "bid"|"ask")`: ignore non-dicts, non-finite,
price not in (0, 1), size ≤ 0. Bid = max price, ask = min price. Shared by
WS cache and pathlog so REST and WS mean the same thing.

**`buy/clob_book_ws.py`**

`wss://ws-subscriptions-clob.polymarket.com/ws/market`. Subscribe with
`assets_ids` + `custom_feature_enabled`. Ping every 8s. Reconnect with
backoff. `get_book_feed()` is a process-wide singleton.

**`buy/entry_skip.py`**

§15. 15m/hourly bots do **not** import this.

**`buy/chain.py` / `buy/contracts.py`**

Mint-only. `splitPosition` into equal UP and DOWN. **Do not run mint live.**

---

## 22. Pathlog (no orders)

`pathlog.py` is allowed to have `if __name__` style usage (it is a normal
script with a lock and a loop; it does not import the buy bots).

Every ~1s, for each series (5m whole window; last 8 minutes of 15m; last 15
minutes of hourly), REST `/book` both legs, append a JSON line with bid/ask
**and size**. After expiry, stamp `winner` from Gamma.

Kill switch: `touch STOP_PATHLOG` in the repo.

Prune: oldest JSONL first, skip files written in the last 2 minutes, stop at
14 days **or** 400 MB. Look for `pathlog_prune` in `pathlog.log`. Export
(`check_path_backtest.py --csv` or `scp` the directory) **before** prune.
This is the one state-like tree that is allowed to delete itself. Do not
`rm` it by hand.

**`check_path_backtest.py`:** first tick that matches an ask band and TTM
window is a “hit.” Paper hedge walks later ticks with the **example JSON**
hedge (5m: 50/55/15) using mid as GUI when spread ≤ 10¢. Pathlog has **no**
last-trade, **no** BTC/PTB, **no** POST latency.

**`--sweep` / `--compare` do not replay the early ≥90 / ≥95 union.** They
use `buy_threshold` / `early_buy_max_price` / `buy_start_s` (late 75–99 /
120s) plus paper hedge keys. `buy_max_price` 0.90 is the early ≥90 floor,
not the paper late cap. `live_5m_paper` is that late rule, not the full live
bot. Extra combos (`band_75_90`, `window_240s`) are one-knob variants, still
not the union.

`--series 5m` must not match filenames containing `15m` (the letters `5m`
appear inside `15m`). The filter is exact-series, not a substring.

---

## 23. Tests (the bots cannot be imported)

CI (`.github/workflows/test.yml`): Python 3.12, `pip install -r
requirements.txt`, `py_compile` the scripts, `unittest discover -s tests`.

| File | Approach |
|---|---|
| `test_buy_skips.py` | Imports `buy.entry_skip` directly |
| `test_buy_fill_shapes.py` | `ast.parse(buybot.py)` and exec selected function sources into a fake namespace |
| `test_path_backtest.py` | Imports `check_path_backtest` (has a `__main__` / functions) |
| `test_book.py` | Imports `buy.book` |
| others | Import the corresponding `check_*.py` or widget parsers |

When you add a helper to the 5m bot that 15m also needs, either put it in
`buy/` (importable) or copy it and teach `test_buy_fill_shapes` to extract
it from **all three** files (several tests already loop the siblings).

---

## 24. Operations: systemd, CI, disk

**Start/stop (operator):**

```bash
systemctl is-active polybuybot polybuybot5m polybuybothourly polymintbot polypathlog
# expect: inactive  active  inactive  inactive  active

sudo systemctl restart polybuybot5m   # after a code pull you trust
```

CI deploy (push to `main` touching bots / `buy/` / `pathlog.py` /
`check_path_backtest.py` / `requirements.txt`): SSH `git pull` + `pip
install`. **No `systemctl restart`.** A blind restart would start disabled
15m/hourly/mint. A merged bugfix does nothing until you restart 5m.

**Disk (July 2026):** `/var/log` filled a 10GB disk; app JSON was ~5MB. Cap
the journal with `deploy/journald-size.conf` (`deploy/DISK_OPS.md`). Pathlog
cap is separate and in-app.

**After merging this 5m-band/hedge work:**

```bash
cd ~/poly-money-maker && git pull
python3 -c 'import json; from pathlib import Path; p=Path("strategy_buy5m.json"); d=json.loads(p.read_text()); d["hedge_threshold"]=0.50; d["hedge_require_ask_max"]=0.55; p.write_text(json.dumps(d, indent=2)+"\n")'
sudo systemctl restart polybuybot5m
```

Watch `buy_attempt` `band=early` / `early_95` and `hedge_attempt` once held
bid ≤ 50¢.

---

## 25. Error handling and the `known_cost` stall

Philosophy: **the process stays up.** Missing data → skip. Ambiguous POST →
quarantine. Unexpected exception → log and continue.

**Per-market `try`:** a bug in market A’s buy path must not skip market B’s
hedge in the same second. Outer `cycle_error` still means “something outside
that loop failed” (refresh, GC, UI).

**13–19 August 2026:** 3294/3294 live 5m `cycle_error`s were
`NameError: name 'known_cost' is not defined`. Inside `buy_uncertain`
handling, `known_cost` was used before assignment (`#80`). That exception
was then the **outer** handler, so **every later market in the poll was
skipped**, hedges included. Isolation (per-market try) is the guard for the
*next* NameError. The assignment fix did nothing until
`systemctl restart polybuybot5m` at 09:42Z on 19 Aug — CI had already
pulled the code hours earlier.

`known_cost` must still be assigned before `spend_cap` uses it. Tests in
`test_buy_fill_shapes.py` grep the source for that order.

---

## 26. Landmines

1. Three copies, not a library. `entry_skip` is 5m-only.
2. 5m is seconds; 15m/hourly are minutes. Define `seconds_left` on 5m.
3. `btc-updown` prefixes `btc-updown-5m`. Slug excludes are load-bearing.
4. GC par fallback assumes winner-leg inventory.
5. Never `import buybot5m`.
6. Tick `0.001` vs `0.01`.
7. Never delete live JSON / logs / `.env`. Export pathlog before prune.
8. Ask ≠ price. Tight REST book required.
9. Bid-alone is not a reversal. Toxic dump still requires bid ≤ threshold.
10. `git pull` ≠ running new code.
11. Live JSON hedge keys override code defaults.
12. `--sweep` is not the live early-band union.
13. Banner ASCII art still mentions old 97¢ / 65¢ — ignore it; JSON is truth.
14. `hedge_min_price` is leftover config, not a FAK floor.

---

## 27. Removed / historical

Deleted in the 2026-08 cleanup (git history only): sell-side `bot*.py`,
on-chain mint **buyer** `buy/runner.py`, paper `sim/`. `mintbot.py` remains
as a **paused** helper, not that runner.

Take-profit (“sell when up $0.30”) was evaluated in 2026-08 and **not**
implemented. `sell_market_with_retry` stays hedge-only.

---

## 28. Glossary

| Term | Meaning |
|---|---|
| **Ask** | Lowest price someone will sell the token for (we buy here) |
| **Bid** | Highest price someone will pay (we hedge-sell here) |
| **CLOB** | Central limit order book (the exchange) |
| **Condition ID** | On-chain market id; state dict key |
| **Daemon thread** | Background thread that dies with the process |
| **FAK** | Fill-And-Kill: take now, cancel the rest |
| **fsync** | Ask the OS to put file bytes on disk for real |
| **Gamma** | Market catalog API |
| **GUI consensus** | Website-style mid-or-last-trade 70/30 rule |
| **Hedge** | Sell-only exit when the held side has truly lost |
| **Hot reload** | Re-read JSON next loop without restarting Python |
| **PTB** | Price To Beat — oracle at market open |
| **Quarantine** | Saved “a POST happened; outcome unknown” |
| **Relayer** | Polymarket submits the redeem transaction for us |
| **REST** | HTTP request/response (here: `/book`, `/order`) |
| **RTDS** | Polymarket live BTC websocket |
| **Spread** | Ask minus bid |
| **TTM** | Time to market end (seconds left on 5m) |
| **Token ID** | CLOB id of UP or DOWN |
| **toxic_fill** | Flag: junk fill; dump only while bid is dead |
| **TWAP** | Time-weighted average price (Chainlink series the 5m market resolves on) |
| **Websocket (WS)** | Push connection; fast cache, not an order |
| **Write-ahead** | Save order id to disk **before** POST |
