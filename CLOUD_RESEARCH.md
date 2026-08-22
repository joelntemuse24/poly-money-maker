# Cloud research agents (no live orders, no `.env`)

Cloud agents paper-score BTC Up/Down **books** against the live 5m template.
They do **not** replace `polybuybot5m`. They never receive `.env`, never
load live `strategy_buy5m.json`, and never start systemd.

The point is **paper P&L** (hedge proceeds or $1 / $0), not “would it have
clicked.” Hits without `pnl` are incomplete.

Pathlog **cannot** replay Polymarket last-trade GUI, Chainlink/PTB, or POST
latency. Paper mode: fill at the recorded ask, walk later ticks for a
70/72/15 hedge (live 5m template; paper is instant, not persist 2s) using
mid-as-GUI when spread ≤ 10¢ (held ≤ 72¢ / other ≥ 28¢; other need not be
ahead). Wide books fail closed. Toxic dumps only while bid ≤ 53¢.

Live books are **public** (Gamma + CLOB). `pathlog.py` records them with no
keys. Do not put `PRIVATE_KEY` on a Cloud Agent.

## 1. Data (optional zip + live recorder)

**Historical tape (best money sample):** attach one archive to the chat
(`poly-research.zip` or `.tgz` from the VM — not the ticks folder). Unpack at
repo root → `pathlog/ticks/*.jsonl`. Do **not** attach `.env` or live JSON.

**Live tape (markets happening now):** run `pathlog.py` in this environment
(GET only). Wait until 5m markets **resolve**, then `--sweep --paper` on
those files. Unresolved markets have no redeem P&L.

Environment install: `.cursor/environment.json` (`python3.12-venv` + pip).
Leave **Start** empty.

## 2. Paste prompt (live paper P&L — use this)

```text
You are a paper P&L research agent for joelntemuse24/poly-money-maker.

Goal: rank strategy variants by money made, not by how often they would fire.
Money = paper P&L after a 70/72/15 hedge (or toxic dump at 53¢) or after redeem at
$1.00 / $0.00. A skip with no fill is $0, not a win. Unresolved markets do
not get a redeem P&L — wait or mark them unresolved.

Hard rules:
- Do not start polybuybot, polybuybot5m, polybuybothourly, or polymintbot.
- Do not create ClobClient with a private key. No POST /order. No relayer.
- Do not read, write, or ask for .env. Do not set dry_run false.
- Do not edit strategy_buy.json / strategy_buy5m.json / strategy_buyhourly.json
  (non-example). Template file is strategy_buy5m.example.json. `--sweep`
  scores the **late** keys only (75–90¢ / last 120s / $2.50). It does
  **not** replay the live early ≥90 / ≥95 union or two $2.50 slices.
- pathlog.py is allowed (recorder only). check_book.py is allowed.
- Gamma GET and CLOB GET only: gamma-api.polymarket.com, clob.polymarket.com.

Setup:
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

If a zip/tgz was attached, unpack at repo root so pathlog/ticks/*.jsonl exists.
Never scrape a substitute for missing ticks by placing orders.

NOW snapshot (30 seconds, public APIs):
- Find the current btc-up-or-down-5m market (and 15m/hourly if open).
- Print slug, seconds left, up/down best bid/ask/size.
- Print whether the live 5m template WOULD BUY on this tick (ask in 75–90,
  ttm ≤ 120, spread ≤ 5¢, one winning leg). This is a call, not P&L yet.

LIVE recorder (markets that are happening):
- Start: .venv/bin/python pathlog.py
  (background). It writes pathlog/ticks, no orders.
- Let it run until at least 6 distinct 5m markets have a resolved winner
  OR 40 minutes wall clock, whichever first. Do not busy-loop the user;
  wait on the process.
- Stop: touch STOP_PATHLOG and wait for pathlog to exit. Do not kill -9
  mid-write if you can avoid it.
- Then score the session tape with paper hedge:

.venv/bin/python check_path_backtest.py --sweep --series 5m
.venv/bin/python check_path_backtest.py --anatomy --series 5m --ttm-max 120
.venv/bin/python check_path_backtest.py --compare --paper --series 5m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --paper --series 5m --budget 15

If older ticks were unpacked from a zip, run the same --sweep/--compare on
that full tape TOO and label tables HISTORICAL vs SESSION.

HISTORICAL extras if ticks exist for those series:
.venv/bin/python check_path_backtest.py --sweep --series 15m --template strategy_buy.example.json
.venv/bin/python check_path_backtest.py --sweep --series hourly --template strategy_buyhourly.example.json
.venv/bin/python check_path_backtest.py --grid --series 5m --budget 2.5

If buybot5m.log exists:
.venv/bin/python check_buy_skips.py --since 2026-08-20T02:46:00

After the tables: at most 5 extra 5m combos that anatomy/grid suggest
(not a cartesian bomb). --paper --series 5m --max-spread 0.05.

How to pick a winner (this is the whole exercise):
- Baseline = live_5m_paper (75–90, 120s, $2.50, paper hedge).
- Rank by pnl_sum first, then win_rate, then hits.
- Ignore a variant with fewer than 5 hits on HISTORICAL or fewer than 3
  fills on SESSION. Lucky n=1 is not an edge.
- Report for baseline and the top 3: hits, full/partial/zero, win_rate,
  pnl_sum, hedges, toxic_dumps, pnl vs baseline.
- Name at most ONE variant that actually made more money than baseline
  with enough hits. If none beat baseline, say so.
- SESSION (this hour) cannot override HISTORICAL by itself. If they
  disagree, report both and do not recommend a live JSON change.
- Pathlog cannot see last-trade GUI, BTC/PTB, or empty FAKs — say that
  next to the recommendation.

Write a draft PR that:
- does NOT change live JSON or bots unless a test/docs bug blocks the sweep
- pastes HISTORICAL and SESSION tables in the PR body
- recommends at most one next live experiment, or “keep 75–90/120s”
- says operator must git pull + systemctl restart polybuybot5m to go live
```

## 3. Tape-only prompt (zip already attached, no waiting)

Use section 2 if you want live markets. This one only scores files on disk.

```text
You are a research agent for joelntemuse24/poly-money-maker. Paper P&L only.

Hard rules:
- Do not start polybuybot, polybuybot5m, polybuybothourly, or polymintbot.
- Do not edit strategy_buy.json / strategy_buy5m.json / strategy_buyhourly.json (non-example).
- Do not read or write .env. Do not set dry_run false. Do not place orders.
- Template file is strategy_buy5m.example.json. `--sweep` scores late
  75–90¢ / 120s / $2.50 plus paper 70/72 (instant) — not the early ≥90 / ≥95 union.
- Rank by paper pnl_sum vs live_5m_paper, not by hit count. If pathlog/ticks
  is missing, run pathlog.py (no orders) instead of inventing books.

Setup:
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

.venv/bin/python check_path_backtest.py --sweep --series 5m
.venv/bin/python check_path_backtest.py --sweep --series 15m --template strategy_buy.example.json
.venv/bin/python check_path_backtest.py --sweep --series hourly --template strategy_buyhourly.example.json
.venv/bin/python check_path_backtest.py --anatomy --series 5m --ttm-max 120
.venv/bin/python check_path_backtest.py --grid --series 5m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --paper --series 5m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --paper --series 5m --budget 15

If buybot5m.log exists:
.venv/bin/python check_buy_skips.py --since 2026-08-20T02:46:00

After the tables: at most 5 extra combos; --paper --series 5m --max-spread 0.05.
Name the money winner vs live_5m_paper (pnl, win_rate, hits, hedges, toxic_dumps)
or say baseline wins. Draft PR: tables only; no live JSON; operator restart to go live.
```

## 4. Automation prompt (Cloud Agents → Automations → on PR)

```text
Paper P&L only. No bots, no .env, no live strategy_*.json.

.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python check_path_backtest.py --sweep --series 5m

If that exits 2 (no ticks), comment: snapshot missing pathlog/ticks.
Otherwise paste the sweep table and name the best variant vs live_5m_paper
by pnl_sum (min 5 hits). Do not merge. Do not edit strategy_buy5m.json.
```

## 5. What “money” means here

| Live | Paper (`--paper` / `--sweep`) |
|---|---|
| Limit FAK at quoted ask, `budget/ask` | Same size model; displayed top is fillable cap |
| GUI + last trade for hedge | Mid if spread ≤ 10¢; 5m held ≤ 55¢ / other ≥ 45¢; wide book = no hedge |
| BTC/PTB side gate | Not replayed (pathlog is books only) |
| Unmatched FAK / POST RTT | Not replayed (optimistic fill at that tick) |
| Toxic dump if bid ≤ 53¢ (5m) | Same from template; recovered bid > 53¢ rides |
| Redeem $1 / wipeout $0 | After no hedge: same — this is the P&L |

`--sweep` is **one change at a time** from the template (window, band, $15,
spread cap on/off, ride vs paper). Extra combos only after those tables, and
only a handful.
