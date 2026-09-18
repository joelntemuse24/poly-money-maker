# BTC 5m dry-run probe ($5)

Hourly (`buybothourly.py` / `polybuybothourly`) and 15m (`buybot.py` /
`polybuybot`) are untouched. This is the **5m** sibling (`buybot5m.py` /
`polybuybot5m`). Joel: do **not** turn live entries on until he says.

Trading oracle is **Chainlink last print vs window-open PTB** — the same
gate `buy/btc_price.py` already uses for 5m/15m. Mid-window restart
REST-backfills PTB from Polymarket `crypto-price` with
`variant=fiveminute` (PR #169). Resolution is Chainlink, not Binance.
Do not point the trading source at TWAP.

`strategy_buy5m.example.json` stays the historical last-120s 75–90¢ /
$2.50 paper template. Do not treat it as this probe.

## Probe knobs (`strategy_buy5m_probe.example.json`)

| Pillar | Probe value | vs 15m |
|---|---|---|
| Window | last **90s** (`buy_start_s=90`) | 15m last **180s**. 90s leaves room for 5s persist + FAK without chasing early 5m chop. 12 windows/hr exist; missing most is fine (~2–3 fills/hr target). |
| Ask band | **0.975–0.99** | 15m **0.95–0.99**. 5m tick is 0.001 so 97.5¢ is a real level; prefer 98–99, allow 97.5. |
| Oracle floor | **$10** Chainlink last vs PTB | Same $10 as 15m (operator range $10–15). |
| Entry persist | **5s** | 15m is 2s. 5s is a longer flicker filter on the 5m tape; still far under hourly 8–20s so it fits a 90s window. |
| Size | **$5** `buy_budget` = `late_buy_budget` = `buy_max_spend` = `market_spend_cap` | Same $5 start. Later ~$40: raise those four plus `buy_max_shares` (≥ spend / 0.975). |
| Overlays | **off** (`early_buy_start_s=90`, `early_95_*=0`, `late_90_start_s=0`) | 15m is already a single sleeve. |
| Hedge book | **0.50 / 0.52** ask, dump **≤0.40**, persist **1s**, dump persist **2s** | 15m is 0.35/0.40. 5m 50/52/40 is the 5m-tuned book; do not blindly copy 15m levels. |
| Dump harden | `ignore_oracle=false`, `require_tight=true`, `require_fresh_book=true`, `edge_collapse_allows_dump=false`, favor-edge block (`max_favor_edge_usd=0`), ladder on, late sweep **15s** | Same post-PR #168 15m intent. Late sweep 15s (15m 20s) because 5m TTM is short. |
| Flatten walks | **on** at `toxic_force_exit_below=0.75` | 5m-tuned; walks below 75¢ dump immediately. |
| Posting | **`dry_run=true` and `entry_enabled=false`** | Same two-knob arm. |

Later ~$40: change `buy_budget`, `late_buy_budget`, `buy_max_spend`,
`buy_max_shares`, `market_spend_cap`. Per-market spend rails and
`max_open_positions` (0 = unlimited) remain. No bot rewrite.

## Dry-run on the VM (Joel-safe)

`polybuybot5m` may already exist as a unit. This PR does **not** enable
or restart it.

```text
cd /home/ntemusejoel/poly-money-maker
cp strategy_buy5m_probe.example.json strategy_buy5m.json
# confirm: dry_run=true, entry_enabled=false, buy_budget=5, buy_start_s=90
.venv/bin/python buybot5m.py
```

Or point at the example without copying:

```text
POLY_BUY5M_STRATEGY=strategy_buy5m_probe.example.json .venv/bin/python buybot5m.py
```

Dry-run writes `buybot5m.dryrun.log`, `positions_buy5m.dryrun.json`, and
`underlying_research_buy5m_dryrun.jsonl`. Watch for `dry_buy` / `dry_sell`
and `buy_skip_*`. **No CLOB POST.**

Now-snapshot (no bot import, Gamma + CLOB GET only):

```text
.venv/bin/python check_5m_probe_now.py
.venv/bin/python check_5m_probe_now.py --strategy strategy_buy5m_probe.example.json
```

Unit file: `deploy/polybuybot5m.service`. Do **not** `systemctl enable` or
`start` it in this task. Merge is not a restart and is not authorization
to arm 5m.

## One-knob later live flip (still off)

Probe ships with both safeties on. `dry_run` is **startup-locked**.

1. In `strategy_buy5m.json` set `"dry_run": false`.
2. Restart `polybuybot5m` (only when Joel says).
3. **One hot-reload knob:** `"entry_enabled": true`.

Real POSTs require `dry_run=false` **and** `entry_enabled=true`. Either
safety alone keeps live posting off. Dry-run still evaluates gates and
logs `dry_buy` even while `entry_enabled` is false.

## Last-120s GTD rest (`entry_rest_gtd`)

Default **false** (code defaults + both example JSONs). When true, the last
120s 97–99 band may **GTD-rest** the GUI tick if the favorite ask is gone
(people hitting the 99¢ bid). Hedge/sell/redeem stay FAK. One rest order per
market, `expiration = end_ts`, cancel by that `order_id` only (never
`cancel_all`). First GTD POST needs `buybot5m.py` on disk + restart; the knob
is hot-reload after that.

## Coherence

`buy/strategy_coherence.py` `validate_5m_strategy_coherence` fail-closes:

- `buy_start_s` in `(0, 300]`
- `entry_book_persist_s` ≤ `buy_start_s`
- `market_spend_cap` ≥ `buy_budget` when cap > 0
- dump ladder + dump < qualify ≤ recovery when those keys are present
