# Cloud research agents (no live orders, no `.env`)

Cloud agents paper-score BTC Up/Down **books** against pathlog ticks.
They do **not** replace live `polymintbot`. They never receive `.env`,
never load live `strategy_mint.json`, and never start systemd.

The point is **paper P&L** (hedge proceeds or $1 / $0), not “would it have
clicked.” Hits without `pnl` are incomplete.

Pathlog **cannot** replay Polymarket last-trade GUI, Chainlink/PTB, or POST
latency. Paper mode: fill at the recorded ask, walk later ticks for a
50/52/15 hedge from the built-in paper knobs (paper honors
`hedge_persist_s`, 1s on that template; dump **40¢**; flatten walks
**<75¢**) using mid-as-GUI when spread ≤ 10¢ (held ≤ 52¢ / other ≥ 48¢;
other need not be ahead). Wide books fail closed. Toxic dumps while bid
≤ 40¢, or flatten while bid < 75¢.

Live books are **public** (Gamma + CLOB). `pathlog.py` records them with no
keys. Do not put `PRIVATE_KEY` on a Cloud Agent.

## 1. Data (optional zip + live recorder)

**Historical tape (best money sample):** attach one archive to the chat
(`poly-research.zip` or `.tgz` from the VM — not the ticks folder). Unpack at
repo root → `pathlog/ticks/*.jsonl`. Do **not** attach `.env` or live JSON.

**Live tape (markets happening now):** run `pathlog.py` in this environment
(GET only; live `SERIES` is **15m only**). Wait until 15m markets
**resolve**, then `--sweep --paper` on those files. Unresolved markets have
no redeem P&L. Do not start `pathlog_hourly_dense`. Historical 5m tick
archives can still be scored with `--series 5m`.

Environment install: `.cursor/environment.json` (`python3.12-venv` + pip,
plus optional `poly-vm` SSH when `POLY_VM_SSH_KEY` is set).
**Start** only writes that optional SSH key (no bots, no systemd).

## Cursor Cloud → poly-vm SSH

Cloud agents can optionally SSH to the live Google VM as the read-only
`poly-auditor` user. This is for logs, strategy JSON, and check scripts only.
Paper research still works when the secret is unset.

1. In **Cursor Dashboard → Cloud Agents → Secrets**, add Runtime Secret
   `POLY_VM_SSH_KEY` = the `poly-auditor` ed25519 private key (PEM/OpenSSH
   text). Never commit this key, and never paste it into chat or the repo.
2. If the environment uses allowlist egress, allow SSH to `35.228.146.195`.
3. After install or start, `ssh poly-vm` connects as `poly-auditor`
   (`HostName 35.228.146.195`, key `~/.ssh/id_ed25519_poly_auditor`).
4. Still never read `.env`, never POST live orders, and never start/stop
   trading systemd units unless the operator explicitly asks.

Install/start are idempotent: missing `POLY_VM_SSH_KEY` is a no-op; a
second run rewrites the key file and does not duplicate the `Host poly-vm`
block. `start` repeats the same helper so a runtime secret still works when
agents boot from an environment snapshot (install does not rerun).

## 2. Paste prompt (live paper P&L — use this)

```text
You are a paper P&L research agent for joelntemuse24/poly-money-maker.

Goal: rank strategy variants by money made, not by how often they would fire.
Money = paper P&L after a 50/52/15 hedge (or toxic dump at 32¢) or after redeem at
$1.00 / $0.00. A skip with no fill is $0, not a win. Unresolved markets
do not get a redeem P&L — wait or mark them unresolved.

Hard rules:
- Do not start polybuybot, polybuybot5m, polybuybothourly, polymintbot,
  polycomplement, polydangerzone, or pathlog_hourly_dense.
- Do not create ClobClient with a private key. No POST /order. No relayer.
- Do not read, write, or ask for .env. Do not set dry_run false.
- Do not edit live strategy_mint.json. Buybot sources are not in this tree.
  `--sweep` uses built-in paper knobs (75–90¢ / last 120s / $2.50).
- pathlog.py is allowed (recorder only).
- Gamma GET and CLOB GET only: gamma-api.polymarket.com, clob.polymarket.com.

Setup:
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

If a zip/tgz was attached, unpack at repo root so pathlog/ticks/*.jsonl exists.
Never scrape a substitute for missing ticks by placing orders.

NOW snapshot (30 seconds, public APIs):
- Find the current btc-up-or-down-15m market (and 5m/hourly if open).
- Print slug, seconds left, up/down best bid/ask/size.

LIVE recorder (markets that are happening):
- Start: .venv/bin/python pathlog.py
  (background). It writes pathlog/ticks, no orders.
- Let it run until at least 6 distinct 15m markets have a resolved winner
  OR 40 minutes wall clock, whichever first. Do not busy-loop the user;
  wait on the process.
- Stop: touch STOP_PATHLOG and wait for pathlog to exit. Do not kill -9
  mid-write if you can avoid it.
- Then score the session tape with paper hedge:

.venv/bin/python check_path_backtest.py --sweep --series 15m
.venv/bin/python check_path_backtest.py --hedge-sweep --series 15m --budget 2.5
.venv/bin/python check_path_backtest.py --anatomy --series 15m --ttm-max 120
.venv/bin/python check_path_backtest.py --compare --paper --series 15m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --paper --series 15m --budget 15

If older ticks were unpacked from a zip, run the same --sweep/--compare on
that full tape TOO and label tables HISTORICAL vs SESSION. Historical 5m
archives: add --series 5m on the same commands.

After the tables: at most 5 extra 15m combos that anatomy/grid suggest
(not a cartesian bomb). --paper --series 15m --max-spread 0.05.

How to pick a winner (this is the whole exercise):
- Baseline = live_5m_paper (built-in knobs: 75–90, last 120s, $2.50, paper 50/52).
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
- recommends at most one next live experiment, or “keep mint-only”
- does not tell anyone to enable buybots; live mint/pathlog restarts are operator-only
```

## 3. Tape-only prompt (zip already attached, no waiting)

Use section 2 if you want live markets. This one only scores files on disk.

```text
You are a research agent for joelntemuse24/poly-money-maker. Paper P&L only.

Hard rules:
- Do not start polybuybot, polybuybot5m, polybuybothourly, polymintbot,
  polycomplement, polydangerzone, or pathlog_hourly_dense.
- Do not edit live strategy_mint.json. Buybot sources are not in this tree.
- Do not read or write .env. Do not set dry_run false. Do not place orders.
- `--sweep` uses built-in paper knobs (75–90¢ / last 120s / $2.50 plus paper 50/52).
- Rank by paper pnl_sum vs live_5m_paper, not by hit count. If pathlog/ticks
  is missing, run pathlog.py (no orders) instead of inventing books.

Setup:
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

.venv/bin/python check_path_backtest.py --sweep --series 15m
.venv/bin/python check_path_backtest.py --anatomy --series 15m --ttm-max 120
.venv/bin/python check_path_backtest.py --grid --series 15m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --paper --series 15m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --paper --series 15m --budget 15

After the tables: at most 5 extra combos; --paper --series 15m --max-spread 0.05.
Name the money winner vs live_5m_paper (pnl, win_rate, hits, hedges, toxic_dumps)
or say baseline wins. Draft PR: tables only; no live JSON; operator restart to go live.
```

## 4. Automation prompt (Cloud Agents → Automations → on PR)

```text
Paper P&L only. No bots, no .env, no live strategy_*.json.

.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python check_path_backtest.py --sweep --series 15m

If that exits 2 (no ticks), comment: snapshot missing pathlog/ticks.
Otherwise paste the sweep table and name the best variant vs live_5m_paper
by pnl_sum (min 5 hits). Do not merge. Do not edit strategy_mint.json.
```

## 5. What “money” means here

| Live | Paper (`--paper` / `--sweep`) |
|---|---|
| Limit FAK at quoted ask, `budget/ask` | Same size model; displayed top is fillable cap |
| GUI + last trade for hedge | Mid if spread ≤ 10¢; held ≤ 52¢ / other ≥ 48¢ from built-in knobs; wide book = no hedge |
| BTC/PTB side gate | Not replayed (pathlog is books only) |
| Unmatched FAK / POST RTT | Not replayed (optimistic fill at that tick) |
| Toxic dump if bid ≤ 40¢; flatten walks while bid < 75¢ | Same from built-in template; recovered bid ≥ 75¢ rides |
| Redeem $1 / wipeout $0 | After no hedge: same — this is the P&L |

`--sweep` is **one change at a time** from the template (window, band, $15,
spread cap on/off, ride vs paper). Extra combos only after those tables, and
only a handful.
