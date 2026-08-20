# Cloud research agents (no live orders)

Cloud agents score **recorded CLOB paths** against the live 5m template.
They do **not** replace `polybuybot5m`. They never receive `.env`, never
load live `strategy_buy5m.json`, and never start systemd.

Pathlog **cannot** replay Polymarket last-trade GUI, Chainlink/PTB, or POST
latency. Paper mode is the closest offline model: fill at the recorded ask,
then walk later ticks for a 35/40/15 hedge using mid-as-GUI when spread ≤ 10¢.
Wide books fail closed (no last print). Toxic dumps only while bid ≤ 35¢.

## 1. One-time: put ticks on the environment disk

On the **VM** (prune deletes JSONL after 14 days / 400 MB):

```bash
cd ~/poly-money-maker
tar czf /tmp/poly-research.tgz pathlog/ticks buybot5m.log
```

Copy `/tmp/poly-research.tgz` off the box. The Cloud Agent **cannot** read
your laptop Downloads folder. Attach `poly-research.tgz` to the Cloud Agent
chat (paperclip), then ask it to unpack at the repo root so you have
`pathlog/ticks/*.jsonl` and `buybot5m.log`, then **save a snapshot**. Refresh
the snapshot when you want a newer sample.

Do **not** copy `.env`, `strategy_buy5m.json`, or `positions_buy*.json`.

Environment install (`.cursor/environment.json`): `python3.12-venv`, then
venv + `pip install -r requirements.txt`. Leave **Start** empty so agents
never launch the live bots.

## 2. Launch prompt (paste into a new Cloud Agent)

```text
You are a research agent for joelntemuse24/poly-money-maker. Paper trading only.

Hard rules:
- Do not start polybuybot, polybuybot5m, polybuybothourly, or polymintbot.
- Do not edit strategy_buy.json / strategy_buy5m.json / strategy_buyhourly.json (non-example).
- Do not read or write .env. Do not set dry_run false. Do not place orders.
- Template is strategy_buy5m.example.json (75–90¢, 120s, $2.50, 35/40/15, GUI on).
- If pathlog/ticks is missing, stop and say the snapshot needs a VM export. Do not scrape live CLOB as a substitute.

Setup:
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

Then score EVERY recorded series, using the live template as the base, paper hedge on:

.venv/bin/python check_path_backtest.py --sweep --series 5m
.venv/bin/python check_path_backtest.py --sweep --series 15m --template strategy_buy.example.json
.venv/bin/python check_path_backtest.py --sweep --series hourly --template strategy_buyhourly.example.json
.venv/bin/python check_path_backtest.py --anatomy --series 5m --ttm-max 120
.venv/bin/python check_path_backtest.py --grid --series 5m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --paper --series 5m --budget 2.5
.venv/bin/python check_path_backtest.py --compare --paper --series 5m --budget 15

If buybot5m.log exists:
.venv/bin/python check_buy_skips.py --since 2026-08-20T02:46:00

After the tables: pick at most 5 extra combos that anatomy/grid suggest (not a
cartesian bomb). Run them with --paper --series 5m --max-spread 0.05. Compare
each to live_5m_paper (hits, win_rate, pnl, hedges, toxic_dumps).

Write a draft PR that:
- does NOT change live JSON or bots unless a test/docs bug is blocking the sweep
- pastes the sweep tables in the PR body
- recommends at most one next live experiment, with what pathlog cannot see
- says operator must still git pull + systemctl restart polybuybot5m to go live
```

## 3. Automation prompt (Cloud Agents → Automations → on PR)

```text
Paper research only. No bots, no .env, no live strategy_*.json.

.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python check_path_backtest.py --sweep --series 5m

If that exits 2 (no ticks), comment on the PR: snapshot missing pathlog/ticks.
Otherwise paste the sweep table as a PR comment and name the best variant vs
live_5m_paper. Do not merge. Do not edit strategy_buy5m.json.
```

## 4. What “realistic” means here

| Live | Paper (`--paper` / `--sweep`) |
|---|---|
| Limit FAK at quoted ask, `budget/ask` | Same size model; displayed top is fillable cap |
| GUI + last trade for hedge | Mid if spread ≤ 10¢; wide book = no hedge |
| BTC/PTB side gate | Not replayed (pathlog is books only) |
| Unmatched FAK / POST RTT | Not replayed (optimistic fill at that tick) |
| Toxic dump if bid ≤ 35¢ | Same; recovered bid > 35¢ rides |
| Redeem $1 / wipeout $0 | After no hedge: same |

`--sweep` is **one change at a time** from the template (window, band, $15,
spread cap on/off, ride vs paper). That is the search space. Extra combos are
allowed only after those tables, and only a handful.
