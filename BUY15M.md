# BTC 15m dry-run probe ($5)

Hourly (`buybothourly.py` / `polybuybothourly`) is untouched. This is the
**15m** sibling (`buybot.py` / `polybuybot`). Joel: do **not** turn live
entries on until he says.

Research backing (ge95 last 180s; skip 90–94 midband):
`docs/15m-regime-panel-canonical.md`. Historical sweep:
`check_15m_regime_panel.py` (public APIs only; writes `exports/` +
`buy_data_15m/`).

Trading oracle is **Chainlink last print vs window-open PTB** — the same
gate `buy/btc_price.py` already uses for 15m. Resolution is Chainlink, not
Binance. Do not point the trading source at TWAP (`SOURCE_TWAP_*` is
logging-only and fail-closes).

## Probe knobs (`strategy_buy15m_probe.example.json`)

| Pillar | Probe value |
|---|---|
| Window | last **3.0 min (180s)** |
| Ask band | **0.95–0.99** (ge95 only; FAK pins the live ask) |
| Oracle floor | **$10** Chainlink last vs PTB |
| Entry persist | **2s** |
| Size | **$5** `buy_budget` = `buy_max_spend` = `market_spend_cap`; open/daily notional **$5** |
| Hedge | **0.35 / 0.40** ask, persist **1s**, dump persist **2s**, oracle required |
| Soft-edge | **off** (load rejects max ≥ floor if turned on) |
| Take-profit | full-lock at **0.99** (15m tick is 0.01; half +4¢ **off**) |
| Early-hot | **off** (load rejects `true`) |
| Sleeves | **one** (`buy_threshold` / `buy_window_min`; no a22/b15) |
| Posting | **`dry_run=true` and `entry_enabled=false`** |

Later live $1–2 test or ~$20 cap: change `buy_budget`, `buy_max_spend`,
`buy_max_shares`, `market_spend_cap`, `max_open_notional`,
`max_daily_notional`. No bot rewrite.

`strategy_buy.example.json` stays the historical 90–96¢ / $10 paper
template. Do not treat it as this probe.

## Dry-run on the VM (Joel-safe)

`polybuybot` is **disabled / inactive** on the live VM (2026-09-13). This
PR does not enable it.

```text
cd /home/ntemusejoel/poly-money-maker
cp strategy_buy15m_probe.example.json strategy_buy.json
# confirm: dry_run=true, entry_enabled=false, buy_budget=5
.venv/bin/python buybot.py
```

Or point at the example without copying:

```text
POLY_BUY15M_STRATEGY=strategy_buy15m_probe.example.json .venv/bin/python buybot.py
```

Dry-run writes `buybot.dryrun.log`, `positions_buy.dryrun.json`, and
`underlying_research_buy_dryrun.jsonl`. Watch for `dry_buy` / `dry_sell`
and `buy_skip_*`. **No CLOB POST.**

Now-snapshot (no bot import, Gamma + CLOB GET only):

```text
.venv/bin/python check_15m_probe_now.py
.venv/bin/python check_15m_probe_now.py --strategy strategy_buy15m_probe.example.json
```

Unit file: `deploy/polybuybot.service`. Do **not** `systemctl enable` or
`start` it in this task. Merge is not a restart and is not authorization
to arm 15m.

## One-knob later live flip (still off)

Probe ships with both safeties on. `dry_run` is **startup-locked**.

1. In `strategy_buy.json` set `"dry_run": false`.
2. Restart `polybuybot` (only when Joel says).
3. **One hot-reload knob:** `"entry_enabled": true`.

Real POSTs require `dry_run=false` **and** `entry_enabled=true`. Either
safety alone keeps live posting off. Dry-run still evaluates gates and
logs `dry_buy` even while `entry_enabled` is false.

## Coherence

`buy/strategy_coherence.py` `validate_15m_strategy_coherence` fail-closes:

- soft-edge max strictly below `min_underlying_edge_usd` when both on
- `early_hot_defer_enabled` must stay false
- `market_spend_cap` ≥ `buy_budget` when cap > 0
- `buy_window_min` in `(0, 15]`
