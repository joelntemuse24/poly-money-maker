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

Live trading is **the 5-minute and 15-minute buy bots** (`buybot5m.py` /
systemd `polybuybot5m`, `buybot.py` / `polybuybot`) plus a recorder that
never places orders (`pathlog.py` / `polypathlog`). The hourly bot exists
as a near-copy and is **stopped**. Minting is **paused**. Do not start
hourly or mint unless the operator asks.

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

**Part III — Walking `buybot5m.py` (hourly/15m copies are stopped)**

8. [How to read the live single-file bot](#8-how-to-read-the-live-single-file-bot)
9. [Startup: lock, secrets, rounding, CLOB client](#9-startup-lock-secrets-rounding-clob-client)
10. [Strategy JSON and hot reload](#10-strategy-json-and-hot-reload)
11. [Logging, notifications, tiny helpers](#11-logging-notifications-tiny-helpers)
12. [Positions: Data API vs local ledger](#12-positions-data-api-vs-local-ledger)
13. [Quotes: websocket vs REST](#13-quotes-websocket-vs-rest)
14. [What “the website shows 80¢” actually means](#14-what-the-website-shows-80-actually-means)
15. [Buy gates and the 5m / hourly price bands](#15-buy-gates-and-the-5m--hourly-price-bands)
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

**Live 5m entry (one $10 FAK, 90–96 last-60s overlay):**

| Time left until close (TTM) | Winning ask | Slice / FAK limit |
|---|---|---|
| More than 60 seconds | none — too early | early and ≥95 are **off** |
| 0 < TTM ≤ 60 seconds | **90¢ through 96¢ inclusive** | late `$10`, limit **96¢** |
| Any TTM | ≥97¢ | **off** (`late_90_start_s=0`) |

**Live 15m entry (one $10 FAK, last 3 min):** winning ask **90–96¢**,
FAK at the live ask. Hedge stays inverted **35/40**.

Live `min_underlying_edge_usd` is **$0** (any non-zero last print vs PTB).
The example JSON / `--sweep` template matches this overlay (last **60s**
90–96 $10, dump persist 2s). `CURRENT.md` wins for what is actually
running. `BUY_HORIZON_S` is **60** on the live overlay.
`early_95_start_s=0` is a valid disable.

Missed early does **not** become a $5 late buy — there is no early slice
while those windows are off. Same-leg add only. After `hedge_closed`, no
re-entry.

**Hedge (5m):** persist **1s @ 50/52** still needs the oracle against/flat
(Chainlink last vs PTB, $0 edge, missing/stale holds). Dump **≤40¢** is
book-only even if BTC still agrees, but must stay ≤40 for **2s**
(`hedge_dump_persist_s`) so a one-tick V-reversal rides. Walks
avg <75¢ flatten at the live bid while bid <90¢. After
persist, sell at the live bid while **< 53¢**, including a fade through 50.
Bid ≥ **53¢** holds and clears persist. Last-30s persist 58 is **off**.
Do not sell 55–69 after persist.
No profit-take sell. Winners redeem at $1.00.

**Hourly is stopped.** Do not start it. Its last-20m 75–90 / $10 /
persist 50/52 / dump 35 / Binance $10 buy-edge code remains in
`buybothourly.py` for a later operator start. **15m is live** after
the paste (last 3 min 90–96 / $10 / inverted 35/40).

**Complement (`complementbot.py`) is a second Polymarket account.** It
does not change 5m/15m sell-hedge. After a confirmed primary fill it
lifts the other token at ≥80¢ (FAK 99¢, share-match). Same-wallet
start is refused. Do not start `polycomplement` without
`.env.complement`. A complement POST is never treated as a full fill
from the request size: confirm `size_matched` / GET-order, persist
`buy_uncertain` **before** the FAK, and cool empty/reject so a miss
does not retry every look.

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
| `polybuybot5m` | `buybot5m.py` | **yes — places orders (after operator restart)** |
| `polypathlog` | `pathlog.py` | **yes — GET books only** |
| `polybuybot` | `buybot.py` | **yes — start after paste** |
| `polybuybothourly` | `buybothourly.py` | stopped |
| `polymintbot` | `mintbot.py` | paused |

Unit files live in `deploy/` and are copied to `/etc/systemd/system/`. The
5m unit loads `.env` (secrets). Pathlog’s unit does **not** — it needs
no keys.

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
| `buybot5m.log` | One JSON object per line (5 MiB × 3 backups) |
| `buybot5m.journal.jsonl` | 5m money-path tape (`check_live_journal.py`) |
| `.heartbeat_buy5m` | Unix time; if it stops moving, the loop is stuck |
| `ptb_chainlink_buy5m.json` | Cached last-print Price To Beat per 5m market |
| `underlying_research_buy5m.jsonl` | Buy/skip/oracle audit for later analysis |
| `pathlog/ticks/*.jsonl` | Recorded books (auto-deleted after 14 days or 400 MB) |

Templates you **may** commit: `strategy_buy5m.example.json` (and the
stopped hourly/15m twins). The bot **never** loads `.example`. The stopped
hourly bot does not write a journal.

`dry_run: true` points state/log/PTB at `*.dryrun.*` names so a paper process
cannot smash live files. `dry_run` is chosen at **startup** because those
paths are picked once. Other knobs hot-reload.

---

## 3. Map of the repository

```
buybot5m.py          Live bot. Last 120s 75–90, edge $10, persist 1s @ 50/52, dump 40, flatten walks <75.
buybothourly.py      Hourly near-copy. Stopped.
buybot.py            15m near-copy. Stopped.
buy/                 Importable helpers (safe — they do not start trading)
  market.py          Find markets on Gamma
  btc_price.py       Chainlink / Binance websocket + PTB
  clob_book_ws.py    Fast top-of-book websocket
  book.py            Parse bid/ask levels (price + size)
  entry_skip.py      5m two-slice + hourly three-slice band math (not 15m)
  hedge_gate.py      5m + hourly persist, recovery, retry, and tick helpers
  live_journal.py    5m-only money-path journal filter (hourly does not use it)
  chain.py           Mint helper: Polygon eth_call (paused path)
  contracts.py       Mint helper: splitPosition calldata (paused path)
pathlog.py           Recorder, no orders
check_*.py           Offline / log diagnostics (`check_reversal_features.py`
                     is Binance |dist| / TTM paper, no orders)
tests/               unittest (CI)
deploy/              systemd units + disk notes
widget/polydesk.py   Laptop glance (public API, no orders)
```

**Rule of copies:** a buy/hedge/quarantine bug in `buybothourly.py` may
exist in the other two files. Diff the siblings after a logic change, but do
not copy cadence-specific math blindly. The 5m bands use `BUY_HORIZON_S` in
**seconds**. Hourly bands use `BUY_HORIZON_MIN` in **minutes**. Stopped hourly
defaults are B-only for the last 20 minutes, persist **5s @ 50/52**, dump
35¢, recovery **53¢**, `hedge_sell_fade`, `hedge_require_oracle`, Binance
buy edge $10, tick `0.01`, and a $10 market cap. Hourly is **stopped**.

Live 5m JSON (on the VM after the 90–96 paste; **`CURRENT.md` wins**)
is last **60s**, **90–96¢**, `min_underlying_edge_usd` **$0**,
`late_90` / early / ≥95 **off**, one $10 FAK, persist **1s @ 50/52**,
dump **40¢** hold **2s**, flatten walks **avg <75¢** at live bid **<90¢**,
recovery 53¢, last-30s ladder **off**, tick `0.001`. The example JSON
and `--sweep` template match that overlay. Code defaults in
`buybot5m.py` are last-60 / edge $0 / dump persist 2s; live JSON still
overlays them until the operator paste.

15m/hourly windows are in **minutes** (`buy_window_min` / `a22_window_min`).
Mixing the two without converting units has caused production `NameError`s.
The 5m loop **must** compute `seconds_left`. Hourly must **not** copy
`seconds_left = (end_ts_ms - now_ms) / 1000`. If someone ports a 15m snippet
that only defines `minutes_left` into 5m, the 5m process dies every cycle.

---

## 4. Binary markets, books, and Fill-And-Kill

**Token price** is a probability on (0, 1). 0.80 means 80¢. Hourly and 15m
markets tick in whole cents (`0.01`). The live 5m bot defaults to
`0.001`, although execution must honor a coarser tick reported by the CLOB.

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
junk. The live 5m bot sizes **shares** from `budget / ask`, then clips
to `buy_max_shares` and `buy_max_spend` so a 99¢ early FAK is at least
3 shares when `$2.97` fits (`buy_max_spend` $3 / `buy_max_shares` 5).
Late 75–90 posts at **90¢**; last-45 ≥90 posts at **99¢**. Early ≥90 is
**off** on the live 5m paste.
Unfilled dollars die. The stopped hourly bot uses the same limit-FAK
sizer at a $10 cap; 15m still limits at the touch.

**Displayed size is not a cap.** If the top ask shows 0.4 shares, live 5m
still posts its legal share/limit FAK. The exchange fills what exists
between the touch and that cap; the remainder dies. Thin books log
`[THIN ASK]`.

---

## 5. Python shape of the bots

### One file, one process, one `while True`

There is no web server, no database, no `asyncio`. The live 5m bot is a script
that:

1. does a lot of setup at **import time** (the moment Python starts the file),
2. then sits in `while not _shutdown_requested:` forever.

**There is no `if __name__ == "__main__"`.** In many Python tutorials that
guard means “only run when I type `python buybothourly.py`, not when I
`import buybothourly`.” These bots skip it on purpose: the file **is** the
program. Consequence: **importing any buy-bot file starts it.** Tests must
not import the bots. They import `buy/` and `check_*.py`, or they **cut
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
The path is `/tmp/poly-money-maker-buybot5m.lock`. If you run
`python buybot5m.py` while systemd already runs it, the second process
**exits**. Two live instances would both think they may spend $2.50.

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
| **RTDS** | Live data websocket | Last-print BTC for trading gates (5m/15m: Chainlink BTC/USD last). TWAP topics exist for resolution logging only |
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
must **not** retry another full slice.

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
order** instead of posting another full slice. This is the most important
money-safety pattern in the file.

---

## 8. How to read the live single-file bot

Live file is `buybot5m.py`. The stopped hourly and 15m copies use the
same comment banners; cadence-specific constants differ (seconds vs
minutes, Chainlink last-print vs Binance, tick `0.001` vs `0.01`).

`buybot5m.py` is one long scroll with **comment banners** as chapters:

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

The stopped hourly and 15m files have the same shape with cadence-specific
constants. Do not start them.

---

## 9. Startup: lock, secrets, rounding, CLOB client

**Order at import (this is the real “main”):**

1. `load_dotenv()` — fill `os.environ` from `.env`.
2. Process lock (above). If this exits, nothing else runs.
3. Read env vars into module globals. Missing keys stay `None` until a later
   check.
4. Patch `ROUNDING_CONFIG`: CLOB taker (shares) max **four** dp; BUY maker
   (USDC) max **two**. Every tick’s `amount` is clamped to 4; hourly also
   sets tick `0.01` `amount` to **2**. Without that, a dirty float can sign
   a maker amount with more than two decimals and the CLOB 400s
   `invalid amounts`.
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
background threads. Then the ASCII banner (its “97¢ winner / hedge @ 65¢”
text is **cosmetic leftover** — live knobs are 75–90¢ / persist 1s @
50/52 / dump 40 / flatten walks). Then
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
- Then a wall of validators: 5m windows and FAK caps are coherent,
  `early_95_start_s=0` is a legal disable (non-negative, not “must be
  positive”; `0` also skips the `start >= min` check so leftover
  `early_95_min_s=60` cannot take entries down),
  `hedge_require_ask_max >= hedge_threshold`, dump < qualify ≤ recovery,
  tick is a CLOB tick and equals this bot’s `EXPECTED_TICK_SIZE`,
  `one_entry_per_market` stays true, live mode cannot disable hedge,
  `buy_max_shares` is large enough for
  `max(budget, buy_max_spend) / buy_threshold` ($2.50 / 75¢ needs 5;
  $5 needs **7**), `max_open_positions == 0` means unlimited, no NaNs.

**Hot reload vs restart:**

| Change | Needs restart? |
|---|---|
| Most knobs in live JSON | No — next loop iteration |
| `dry_run` | **Yes** (state paths) |
| Python code | **Yes** (`systemctl restart polybuybot5m`). `git pull` is not a restart. |
| Current 50/52/5s/32/53/oracle values if live JSON has older keys | The **file** wins. Code defaults only apply when a key is omitted. Patch the live JSON before restart. |

`entry_enabled` is the operator arm. Defaults `false` in the example file.
Live JSON on the VM is what actually arms buys.

---

## 11. Logging, notifications, tiny helpers

`log_event("buy_skip", reason="ask_below_band", ...)` writes one JSON object
to `buybot5m.log` via `RotatingFileHandler` (5 MiB × 3 backups). The
Rich console lives in `journalctl -u polybuybot5m` (journald is capped
separately). The 5m bot also writes `buybot5m.journal.jsonl`
(`check_live_journal.py`). **Throttled** skips
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
rows whose slug/event slug matches the 5m `SLUG_PREFIX`
(`btc-updown-5m`) and **not** `SLUG_EXCLUDES`. The excludes prevent
15m (`btc-updown`) and hourly (`bitcoin-up-or-down`) naming from leaking
into the 5m process. Outcome `yes`
maps to UP, `no` to DOWN (Polymarket naming drift).

**The Data API lags.** You can buy, POST returns, local JSON has 11.1 shares,
and Data API still shows 0 for a while. If the hedge loop trusted Data API
alone, it would **skip hedging** a position that exists, or **re-buy** a
market it already owns.

`merge_tracked_positions(api_held, tracked_meta)` therefore **inserts local
confirmed inventory** into the in-memory `held` map when Data API omitted it.
Local `bought_size` after a confirmed **sell** is also a ceiling: Data API
must not resurrect a larger bag.

`add_tracked_market_stubs` keeps expired-but-unsettled markets in the loop
so redeem/GC still run after Gamma drops them from “active.”

`drop_wallet_dust` removes old Data API rows from the hot loop unless they
are a live hedge, a recoverable uncertain order, or real settlement work.
`position_is_live_hedge` drives the hot-path count and polling. Therefore
banner **POS** means live hedge inventory, not the number of historical
wallet rows; redeemable leftovers are settlement backlog.

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
  (the live watch list follows the current 5m horizon and held bags).

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

**Live 5m hedge GUI** matches the 50/52 book, not buy 70/30: held last trade
≤ 52¢, held GUI ≤ 52¢, other GUI ≥ 48¢ (complement). The buy-side 5¢
ambiguity gap does **not** apply here, so a documented 50¢-vs-48¢ book
qualifies. A 49/51 book with a held last trade at 85¢ is a clip, not a
reversal → `hedge_skip_no_consensus`. After GUI, 5m waits
`hedge_persist_s` (1s) on a continuously qualified book
(`hedge_skip_persist` / `buy/hedge_gate.py`). Elapsed wall time alone does
not complete persistence; the endpoint tick must still pass the current
50/52 book and GUI. A bounce or failed endpoint check clears the arm.

`entry_book_ok`: both sides present, not crossed, spread ≤ 5¢, bid ≥ 70¢.

`hedge_book_ok`: bid ≤ threshold (50¢), ask ≤ 52¢, spread ≤ 15¢. A 1¢ bid
under a 99¢ ask fails (`hedge_skip_toxic_book`).

`evaluate_held_bag` treats `bid ≤ hedge_toxic_bid_max` (live 5m **40¢**) as
an immediate, bid-only dump of **any** live bag. It skips GUI and
persistence, so a wide 20/80 can still dump. Live 5m
`hedge_dump_ignore_oracle` lets that dump fire even if Chainlink still
agrees; persist-50 still needs the oracle against/flat. Missing/stale
oracle data holds persist sells. Walk flatten is a **separate** kwargs
path (`flatten` / `flatten_max=0.75`) so dump < qualify ≤ recovery stays
`0.40 < 0.50 ≤ 0.53`. Stopped hourly still dumps at 35¢ after
its Binance oracle allows it.

---

## 15. Buy gates and the 5m / hourly price bands

`buy/entry_skip.py` contains pure entry functions (no network). Tests import
it directly.

**Live 5m** (`applicable_entry_bands` / `decide_5m_entry`) uses **seconds**.
With the live JSON paste:

- late: `0 < TTM ≤ buy_start_s` (**45**) → 75–90¢ inclusive, FAK **90¢**,
  late `$2.50`;
- `late_90`: `0 < TTM ≤ late_90_start_s` (**45**) → ask ≥90¢, FAK **99¢**,
  same late slice;
- early ≥90 is off when `early_buy_start_s == buy_start_s`;
- ≥95 is off when `early_95_start_s=0` (`early_95_s > early_95_floor`
  is false). `0` is legal in `load_strategy`.

`BUY_HORIZON_S` is **45**, so hot poll / WS subscribe start around T-75.
The last-print vs PTB underlying gate lives in the 5m loop, not this
helper. 91–99 at TTM 46–120 is a no because those windows are closed.

**Stopped hourly** (`applicable_hourly_entry_bands`) uses
**minutes** and inclusive windows: `0 < minutes_left ≤ window`.

The last hourly configuration opened only slice B (`b15`):

- `b15_window_min = buy_window_min = 20`;
- ask **75–90¢ inclusive**;
- FAK limit **90¢**;
- `b15_buy_budget = market_spend_cap = $10`.

Windows at or below zero are disabled, so `a22_window_min = 0` and
`c5_window_min = 0` keep A/C off. Their helper logic remains for tests and a
deliberate future experiment: A is >93¢ with a $5 slice and 99¢ limit; C is
>95¢ with a 99¢ limit. If both were open, B has priority for 75–90 and C
has priority above 95. None of that dormant logic changes the live B-only
rule.

`buybothourly.current_entry_bands(minutes_left)` wraps the helper.
`BUY_HORIZON_MIN` is the maximum of the configured hourly windows and
`buy_window_min`; when hourly last ran it was **20**. TTM is
`(market.end_ts - now) / 60`; do not paste the live 5m bot’s
`seconds_left` math into this loop. Hourly stays **stopped**.

`t15_bought` records a B-slice fill. `hourly_spent_so_far` uses durable
`pnl_entry_cost`; `hourly_slice_budget` can spend only what remains under
the $10 market cap. A filled live B slice cannot fire again.
`can_arm_hourly_slice` rejects **every** slice after `hedge_closed`,
including legacy/incomplete state (`buy_skip_hedge_closed`). The generic
multi-slice helper also permits only same-token adds while inventory is
live and logs `buy_skip_other_leg` for a side switch.

**Other gates (all must pass):**

1. `ENTRY_ENABLED`
2. Gamma discovery is a background directory. A stale catalog still
   allows a buy look on a market already in `_cached_markets` with
   tokens + `end_ts` that is inside the live window (5m last-60s /
   15m last-3min / hourly open slice). `stale_discovery` only skips
   markets we never stored. Hedge never waits on Gamma freshness.
3. Fresh positions snapshot and USDC balance
4. No `buy_uncertain` quarantine
5. Slice still unused and market spend still below $10
6. Past `buy_grace_s` after first sighting
7. Open-position, open-notional, and daily-notional rails
8. Cooldown after an empty FAK
9. Website-style winner ≥70¢ / loser ≤30¢, a 5¢ display gap, and a tight
   REST book (spread ≤5¢, winner bid ≥70¢)
10. Winning ask inside the open 75–90¢ band
11. Binance BTCUSDT versus the hourly PTB is at least **$10** from flat and
    favors the same leg. Missing/stale PTB/live data or side disagreement
    skips the buy (`buy_skip_underlying_edge` /
    `buy_skip_underlying_side`). REST confirmation recomputes the CLOB
    winner, then reapplies that favored side so a book flip cannot buy
    against Binance.
12. Immediately before every BUY POST/retry, `hourly_entry_final_gate`
    (wired as `pre_submit` plus `deadline_ts=m.end_ts`) rechecks TTM > 0,
    the selected B band, fresh Binance/PTB still favoring the selected
    leg, `hedge_closed` false, and the selected leg still the CLOB/GUI
    winner. A failed gate aborts without posting.

The entry feed is Binance because that is the hourly market’s resolution
source. The holding-time oracle veto is separate and intentionally uses a
$0 edge: any non-zero tick still on the held side blocks a sell, while flat
or flipped permits the hedge pipeline.

---

## 16. Sizing a buy (Decimal, 2 cents, 4 dp shares)

`quoted_buy_shares(budget, ask, share_cap)` sizes the share count.

`quoted_buy_shares_up_to_limit` starts from that count, clips it to what the
FAK limit and spend cap can legally fund, prefers at least **3.00 shares**
when `3 × limit ≤ spend_cap`, then snaps to a size whose
`size × limit` is exact cents. Live 5m late therefore posts about
**2.77 shares / $2.50** at 90¢ when the ask is 90¢, or **3.00 shares /
$2.97** at the 99¢ last-45 overlay when `$2.97` fits. It may fill cheaper;
unfilled depth is killed. The stopped hourly B slice posts **11.10 shares
/ $9.99** at 90¢.

Do **not** pass `user_usdc_balance` on hourly (or 5m) BUY `OrderArgs`. That
field means wallet balance, not a per-order cap. A fake cap can make the SDK
shrink an otherwise legal size and sign a maker amount with four decimals,
which the CLOB rejects as `invalid amounts`. The stopped 15m path is older
and still passes `remaining_budget`.

1. Quantize budget down to **cents** (`Decimal("0.01")`, `ROUND_DOWN`).
2. Start with `shares = budget / ask`, quantized to **0.01 shares**.
3. Clip to `buy_max_shares` (live 5m: 5; `$5` later needs **7**) and to
   `spend_cap / FAK_limit`.
4. Search in 0.01-share steps for a size whose
   `shares × FAK_limit` is exact cents and within the cap.
5. If no legal pair exists, return `0.0` (no POST).

`Decimal(str(x))` not `Decimal(x)`: `float(2.5)` is already binary-fuzzy;
going through the string of the intended decimal is the usual money pattern.

`buy_fill_walked`: confirmed shares > 1.05 × quoted. That logs
`buy_fill_walk` and, when fill cost is missing, attributes the full maker
USDC. It does **not** arm `toxic_fill`; a limit FAK can receive more shares
when it fills below its cap. In the hourly bot, `classify_buy_fill` arms
`toxic_fill` only when average `< toxic_force_exit_below` (65¢). A
65–74¢ below-band average is logged but stays on the normal hedge path.
Average is `USDC / shares` (`implied_buy_average`), never
“extra shares × gate ask.” A confirmed B fill stamps `t15_bought`; a ghost
with no confirmed inventory must not consume the slice.

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
   - Hourly wires `pre_submit` + `deadline_ts` so every attempt/retry
     re-runs `hourly_entry_final_gate` after the order is built and
     immediately before write-ahead / POST. Expiry, oracle/side flip,
     band close, or `hedge_closed` abort without posting.
   - **`on_submit` write-ahead** (quarantine JSON) **before** POST. If this
     save fails → **do not POST** (`persist_fail`).
   - Sign order; `signed_order_id` hashes EIP-712 typed data so the id is
     **determined before** the network — crash recovery can look it up.
   - POST a limit FAK at the selected band’s cap. Live 5m late uses **90¢**,
     last-45 ≥90 uses **99¢**, and omits `user_usdc_balance`. Stopped hourly
     B uses 90¢; dormant A/C would use 99¢.
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
maybe `toxic_fill`, and stamps the filled slice (`late_bought` or
`early_bought`). With early off, that is one late slice unless a same-leg
add still fits.

**Unmatched empty cooldown:** after a fully empty trigger, wait
`empty_fak_cooldown_s` (0.15s) before the outer loop tries that market
again. Inner retries are immediate.

---

## 18. The hedge (the only sell)

`sell_market_with_retry` docstring says hedge-only. There is no profit-take
caller.

**Live 5m hedge pipeline** (`evaluate_held_bag` in `buy/hedge_gate.py`,
orchestrated by `buybot5m.py`): persist **1s @ 50/52**, dump **≤40¢**
book-only even if Chainlink still agrees (`hedge_dump_ignore_oracle`),
flatten walks (`toxic_fill` + bid **<75¢**) at the live bid before
recovery_cancel, recovery **53¢**, `hedge_sell_fade` (after persist, sell
live bid **< 53¢**, including a fade through 50). Persist still needs the
oracle against/flat. Do not sell 55–69 after persist. Sells use the live
bid on the market tick (honor 0.01 when CLOB says so).

**Stopped hourly** uses the same helper with dump **35¢** and a Binance
oracle that also gates the dump (no `hedge_dump_ignore_oracle` on that
template). Do not start it.

Shared `evaluate_held_bag` steps (live **5m** numbers):

1. **Dump can skip the oracle.** On 5m, bid ≤ **40¢** dumps any live bag
   bid-only even if Chainlink still agrees (`hedge_dump_ignore_oracle`).
   Persist-50 does **not** get that bypass. Walk flatten (`flatten_walk`)
   also dumps at the live bid while bid **<75¢** so a 70¢ walk does not
   HOLD at recovery 53. Missing/stale REST uses
   WS/last-good (`pick_held_quote`).
2. **Oracle veto on persist.** `hold_while_oracle_agrees` reads last live
   BTC versus PTB with a $0 minimum edge. Missing/stale or still
   on the held side → clear persist and hold
   (`hedge_skip_oracle` / `hedge_skip_oracle_still_winning`).
3. Before persistence completes, a fresh bid above 50¢ is a healthy bounce
   and clears the arm. Bid ≥ `hedge_recovery_cancel` (**53¢**) also clears
   a completed arm (`hedge_skip_recovery`).
4. Normal qualify is a tight bid ≤ **50¢**, ask ≤ **52¢**, spread ≤15¢,
   held GUI/last trade ≤52¢, and other GUI ≥48¢. It must remain qualified
   for **1 second**; any failed book/GUI check resets the arm.
5. Once persist completes, `hedge_sell_fade=true` sells at the live bid
   while it remains **below 53¢**, including a fade through 50¢. Bid ≥53¢
   holds and clears persist. Do not sell 55–69. (40¢, 50¢) before persist
   is a dead band except the 40¢ dump (and flatten walks).
6. Write-ahead hedge quarantine, then a sell FAK at the **live bid** on
   the market tick (5m default `0.001`, honor CLOB 0.01 via
   `hedge_tick_retry`). Undercut 0. `hedge_min_price` is leftover config,
   not a FAK floor.
7. Unmatched / invalid-tick retries re-quote. Dump retry stops if bid
   recovers above 40¢. Flatten retry continues while bid <75¢. Persist-done retry continues while bid <53¢.
8. `hedge_closed` only after confirmed inventory is gone.

Stopped hourly uses the same helper with dump **35¢** and **no** dump
oracle bypass. Do not start it. Stopped 15m keeps an older separate path.

**`toxic_fill`:** armed when confirmed average is below 75¢ (live
`toxic_force_exit_below` = `buy_threshold`). Flatten sells that bag at
the live bid while bid **<75¢**. Every live 5m bag still dumps at ≤40¢
whether or not the flag is set. A recovered toxic flag (bid ≥75¢) may
log `hedge_skip_toxic_recovered`.

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

1. Skips redeem HTTP if a live hedge is active or an enabled entry window
   is open — redeem latency must not delay a hedge or buy.
2. Skips if `buy_uncertain` / `hedge_uncertain` (settle execution first).
3. Throttles per condition (`redeem_throttle_s` 30s).
4. Builds `redeemPositions` calldata for the CTF.
5. `submit_proxy_tx`: fetch relayer nonce, encode proxy call, EIP-712 sign
   with `PRIVATE_KEY`, check derived proxy == `FUNDER_ADDRESS`, POST `/submit`
   with Relayer API key headers **or** Builder HMAC.
6. Store `redeem_pending` + `redeem_tx_id`. **This is not P&L.**
7. Background `GET /transaction`. Credit par only when relayer says
   confirmed **and** a complete Data API snapshot shows inventory gone.
8. Permanent precheck/redeem failures can set durable `redeem_abandoned`;
   startup restores those conditions into the in-memory blocklist so a
   zero on-chain ghost does not spam the relayer every cycle.
9. Age out after `max_redeem_age_days` (7).

GC (`gc_can_finalize`): only with terminal evidence (verified redeem value,
hedge closed with dust remainder, or metadata for a market that never
entered). `record_pnl` is idempotent by condition id. `gc_par_redeem`
returns only an explicitly recorded redemption value; GC never invents par
from `bought_size`.

---

## 20. The main loop, one cycle

After import, `while not _shutdown_requested:`:

1. Increment `CYCLE`, snapshot clocks, and hot-reload strategy keys into
   uppercase globals. Recompute `BUY_HORIZON_S` (live 5m) or
   `BUY_HORIZON_MIN` (stopped hourly).
2. Consume and re-kick staggered thread-pool refreshes for positions, USDC
   balance, and Gamma discovery. Write the heartbeat.
3. Filter active, non-`neg_risk`, future markets; merge durable local
   inventory; drop dead wallet dust; add tracked stubs needed for
   reconciliation.
4. Build the fast-path market list and **sort held/quarantined first** so a
   hedge or exact-order recovery runs before new entry I/O.
5. Render the throttled Rich status/positions table (cosmetic).
6. GC stale metadata only when terminal evidence permits it.
7. If there is no active hedge and no enabled 5m entry window, submit
   eligible redemptions with write-ahead intent.
8. Collect prefetched books and overlay fresh WS top-of-book.
9. `for m in _loop_markets:` uses a **per-market try/except**. Inside:
   compute `seconds_left`, reconcile uncertain exact order ids, run the
   oracle/hedge path for held inventory, then run entry gates for an
   eligible open slice. A fault logs `condition_id` and continues to the
   next market. (Stopped hourly uses `minutes_left` instead.)
10. Collect and schedule low-priority redeem-status polling; settle
    redemption value only after relayer confirmation plus a complete
    positions snapshot with inventory gone.
11. An outer `cycle_error` covers failures outside the market loop
    (refresh/GC/UI/redeem). The process stays up.
12. Choose sleep: **0.01s** for a live hedge or market inside
    `BUY_HORIZON_S` (45s after the paste), otherwise 1s. Expired/redeemable leftovers do not
    count as live hedges.
13. Before sleeping, prefetch missing books, set the WS watch list to
    horizon/held tokens (with 30 seconds of slack), and prune REST caches.

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
uses for **new** directory rows. A known live-window market
(`discovery_allows_buy_look`) still reaches the CLOB look when Gamma is
stale. `market_is_known_for_buy` is tokens + `end_ts` already on the
cached object.

**`buy/btc_price.py`**

One `BtcUnderlyingFeed` per source. Daemon thread on
`wss://ws-live-data.polymarket.com`. Ring buffer ~12,000 samples (~3 hours
at 1 Hz). `live_price()` returns `None` if older than 5s (stale). PTB:
nearest tick to `start_ts` within 2s skew, persisted to `ptb_*_buy*.json`.
Missed the open → no PTB → no buy. Trading gates use last-print vs
window-open PTB: 5m/15m subscribe to `crypto_prices_chainlink` / `btc/usd`
and persist `ptb_chainlink_buy5m.json` / `ptb_chainlink_buy.json`. The
stopped hourly bot subscribes to `crypto_prices` / `btcusdt`. TWAP 30s/60s
feeds exist only for optional resolution logging; `underlying_check`
refuses them.

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

§15. Live 5m two-slice band math (seconds) plus stopped hourly
(`a22` / `b15` / `c5`) minutes/budget helpers. 15m does **not** import this.

**`buy/hedge_gate.py`**

Pure hedge persistence, recovery/fade, oracle decision, retry, quote-choice,
and CLOB tick helpers shared by hourly and 5m. It performs no I/O; each bot
orchestrates REST/WS/GUI/oracle calls around the returned `HedgeIntent`.

**`buy/chain.py` / `buy/contracts.py`**

Mint-only. `splitPosition` into equal UP and DOWN. **Do not run mint live.**

---

## 22. Pathlog (no orders)

`pathlog.py` is allowed to have `if __name__` style usage (it is a normal
script with a lock and a loop; it does not import the buy bots).

Every ~1s, for each series (5m whole window; last 8 minutes of 15m; last 20
minutes of hourly), REST `/book` both legs, append a JSON line with bid/ask
**and size**. After expiry, stamp `winner` from Gamma.

Kill switch: `touch STOP_PATHLOG` in the repo.

Prune: oldest JSONL first, skip files written in the last 2 minutes, stop at
14 days **or** 400 MB. Look for `pathlog_prune` in `pathlog.log`. Export
(`check_path_backtest.py --csv` or `scp` the directory) **before** prune.
This is the one state-like tree that is allowed to delete itself. Do not
`rm` it by hand.

**`check_path_backtest.py`:** first tick that matches an ask band and TTM
window is a “hit.” `--template strategy_buy5m.example.json` maps the live
90–96¢ / last **60s** / $10 entry and B+C hedge (persist 1s, dump 40
hold 2s, flatten walks) into paper
knobs. Paper persistence is real: qualifying ticks must stay continuous
for the configured 1 second. Tight books use midpoint as the GUI and
last-trade proxy; displayed top size caps the paper fill. Pathlog has
**no** `|TWAP−PTB|` — the live **$0** edge is not in pathlog.

This is still **not a live 5m replay**. Pathlog has no Chainlink/PTB, no
last trade, no POST latency, and no unmatched FAKs. Paper mode also does
not model `hedge_require_oracle`, `hedge_sell_fade`, recovery-cancel
semantics, or the universal bid-only 40¢ dump / walk flatten exactly. Treat its P&L as a
book-path comparison, not proof that the live bot would have traded.

`--sweep --series 5m` scores one-at-a-time variants of that late template
(`live_5m_paper` is the example JSON last-60 90–96).
`window_120s` is an explicit variant. It does not union last-45 ≥90 or
early ≥90. Always pass `--series 5m` when researching the live cadence.

`--series 5m` must not match filenames containing `15m` (the letters `5m`
appear inside `15m`). The filter is exact-series, not a substring.

---

## 23. Tests (the bots cannot be imported)

CI (`.github/workflows/test.yml`): Python 3.12, `pip install -r
requirements.txt`, `py_compile` the scripts, `unittest discover -s tests`.

| File | Approach |
|---|---|
| `test_buy_skips.py` | Imports `buy.entry_skip` directly |
| `test_discovery_buy.py` | Known last-window + stale Gamma still looks at the CLOB; unknown markets cannot; `MarketGateway.discover` still refreshes |
| `test_buy_fill_shapes.py` | `ast.parse` buy-bot files and exec selected function sources into a fake namespace |
| `test_hedge_persist.py` | Exercises persist, recovery, fade, oracle, and hourly wiring without importing a bot |
| `test_path_backtest.py` | Imports `check_path_backtest` (has a `__main__` / functions) |
| `test_last120_tick_autopsy.py` | Joins `buy_fill` via `token_id` (no slug) and walks live 50/52 dump-32 persist 0/1/2/5s |
| `test_book.py` | Imports `buy.book` |
| others | Import the corresponding `check_*.py` or widget parsers |

When you add a helper that another bot needs, either put it in `buy/`
(importable and no I/O) or copy it and teach extraction tests to check the
affected siblings. Never import a buy-bot module in a test.

---

## 24. Operations: systemd, CI, disk

**Start/stop (operator):**

```bash
systemctl is-active polybuybot polybuybot5m polybuybothourly polymintbot polypathlog
# expect: active  active  inactive  inactive  active

# After checking strategy_buy5m.json / strategy_buy.json dry_run / entry_enabled:
sudo systemctl restart polybuybot5m
sudo systemctl start polybuybot
```

CI deploy (push to `main` touching bots / `buy/` / `pathlog.py` /
`check_path_backtest.py` / `requirements.txt`): SSH `git pull` + `pip
install`. **No `systemctl restart`.** A merged bot change does nothing until
the operator deliberately restarts the affected active unit. Do not start
hourly or mint as part of a 5m/15m deploy.

**Disk (July 2026):** `/var/log` filled a 10GB disk; app JSON was ~5MB. Cap
the journal with `deploy/journald-size.conf` (`deploy/DISK_OPS.md`). Pathlog
cap is separate and in-app.

`CURRENT.md` owns the live knobs so they cannot drift in two places.
Live is last-60 90–96 / $10 / dump-hold 2s (5m) plus last-3min 90–96 /
$10 inverted 35/40 (15m). **Do not paste last-45 + $25.
Do not restart 5m unless the operator asks.** Hedge 50/52 for 5s, dump
32, recovery 53, sell-fade/oracle/dump-ignore true, undercut 0. Stop
hourly. Restart `polypathlog` when recorder Python changes.

Watch `buy_attempt band=late` / `late_90`, `buy_skip_underlying_edge`,
`hedge_skip_oracle_still_winning`, `hedge_skip_persist`,
`hedge_skip_recovery`, `hedge_attempt`, and `hedge_fill` in `buybot5m.log`.

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

1. Three copies, not a library. `entry_skip` and `hedge_gate` serve 5m +
   hourly; 15m keeps older paths.
2. 5m is seconds; 15m/hourly are minutes. Define `seconds_left` on 5m.
   Do not copy that assignment into hourly.
3. Slug filters are load-bearing. Hourly uses
   `SLUG_PREFIX = "bitcoin-up-or-down"` plus excludes for
   `btc-updown-5m` / `btc-updown`; the older `btc-updown` prefix also
   overlaps `btc-updown-5m`.
4. GC must never infer redemption par from a vanished row; require confirmed
   relayer status plus a complete Data API snapshot.
5. Never import any buy-bot module.
6. Tick `0.001` vs `0.01`.
7. Never delete live JSON / logs / `.env`. Export pathlog before prune.
8. Ask ≠ price. Tight REST book required.
9. Normal 5m persist needs oracle + book + GUI + 1s. The 40¢ dump is
   bid-only even if BTC still agrees. Walk flatten skips recovery 53.
10. `git pull` ≠ running new code.
11. Live JSON hedge and entry keys override code defaults.
12. Pathlog `--sweep` `live_5m_paper` is the example JSON late 45s / 75–90
    (no `$25` edge). Score `|dist|` in `check_reversal_features.py`.
13. Banner ASCII art still mentions old 97¢ / 65¢ — ignore it; JSON is truth.
14. `hedge_min_price` is leftover config, not a FAK floor.
15. A/C hourly windows are disabled. Do not revive >93/>95 by copying an old
    template. Do not start hourly.
16. After `hedge_closed`, do not re-enter that market.
17. 5m fade sells in (40¢, 50¢) only after persist when `hedge_sell_fade`
    is on. Dump ≤32 does not wait.

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
| **GUI consensus** | Website-style mid-or-last-trade check: 70/30 for entry; live 5m hedge 52/48 |
| **Hedge** | Sell-only exit when the held side has truly lost |
| **Hot reload** | Re-read JSON next loop without restarting Python |
| **PTB** | Price To Beat — oracle at market open |
| **Quarantine** | Saved “a POST happened; outcome unknown” |
| **Relayer** | Polymarket submits the redeem transaction for us |
| **REST** | HTTP request/response (here: `/book`, `/order`) |
| **RTDS** | Polymarket live BTC websocket |
| **Spread** | Ask minus bid |
| **TTM** | Time to market end (minutes in hourly/15m; seconds in 5m) |
| **Token ID** | CLOB id of UP or DOWN |
| **toxic_fill** | Flag for average below 75¢; live 5m dump ≤40¢ is independent of the flag; flatten sells walks while bid <75¢ |
| **TWAP** | Time-weighted average price (resolution logging only; trading gates use last print vs PTB) |
| **Websocket (WS)** | Push connection; fast cache, not an order |
| **Write-ahead** | Save order id to disk **before** POST |
