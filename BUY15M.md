# BTC 15m dry-run probe ($5) — RETIRED

**2026-09-19:** 15m CLOB buy (`polybuybot` / `buybot.py`) is retired. Live
money path is atomic mint (`polymintbot`). Do **not** enable
`polybuybot`. The unit file is `archive/deploy/polybuybot.service`.

Historical probe notes below are not authorization to run this bot.

Hourly (`buybothourly.py` / `polybuybothourly`) is also retired. This was the
**15m** sibling (`buybot.py` / `polybuybot`).

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
| Entry persist | **2s**, arm floor **0.95** (`entry_persist_min_price`; not the buy band) |
| Size | **$5** `buy_budget` = `buy_max_spend` = `market_spend_cap`. Open/daily notional caps **removed**. |
| Hedge | **0.35 / 0.40** ask, persist **1s**, dump persist **2s**, oracle required |
| Soft-edge | **off** (load rejects max ≥ floor if turned on) |
| Take-profit | full-lock at **0.99** (15m tick is 0.01; half +4¢ **off**) |
| Early-hot | **off** (load rejects `true`) |
| Sleeves | **one** (`buy_threshold` / `buy_window_min`; no a22/b15) |
| Posting | **`dry_run=true` and `entry_enabled=false`** |

Later live $1–2 test or ~$20 cap: change `buy_budget`, `buy_max_spend`,
`buy_max_shares`, `market_spend_cap`. Open/daily notional caps are gone
from the 15m path — do not add them back. Per-market spend rails and
`max_open_positions` (0 = unlimited) remain. No bot rewrite.

`strategy_buy.example.json` stays the historical 90–96¢ / $10 paper
template. Do not treat it as this probe.

Persist (`entry_book_persist_s`) is anti-flash integrity. It arms once the
CLOB ask (GUI only if ask is missing) is ≥ `entry_persist_min_price`
(default **0.95**) and holds through 95→99; it resets if the ask dips
below that floor. The actual buy still needs the existing buy band
(`buy_threshold`…`buy_max_price`, live often ~96.5–99¢) plus persist
ready and the other gates. Live `strategy_buy.json` is gitignored — set
`entry_persist_min_price` on the VM. Hourly is unchanged (same floor can
land there later).

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
and `buy_skip_*`. Live (and dry) order-path attempts also log
`buy_depth_ladder` — visible ask depth / max fill at $5/$10/$20/$32/$40/$50/$100,
same shape as hourly — plus `buy_depth_topup_sim` when the path buffer has
enough samples. **No CLOB POST.**

Now-snapshot (no bot import, Gamma + CLOB GET only):

```text
.venv/bin/python check_15m_probe_now.py
.venv/bin/python check_15m_probe_now.py --strategy strategy_buy15m_probe.example.json
```

Unit file (archived, do not enable): `archive/deploy/polybuybot.service`. Do **not** `systemctl enable` or
`start` it in this task. Merge is not a restart and is not authorization
to arm 15m.

## Later live flip — retired

Do **not** set `dry_run=false`, do **not** restart `polybuybot`, and do
**not** set `entry_enabled=true`. 15m CLOB buy is retired; mint is the
live 15m path.

Real POSTs require `dry_run=false` **and** `entry_enabled=true`. Either
safety alone keeps live posting off. Dry-run still evaluates gates and
logs `dry_buy` even while `entry_enabled` is false.

## Coherence

`buy/strategy_coherence.py` `validate_15m_strategy_coherence` fail-closes:

- soft-edge max strictly below `min_underlying_edge_usd` when both on
- `early_hot_defer_enabled` must stay false
- `market_spend_cap` ≥ `buy_budget` when cap > 0
- `buy_window_min` in `(0, 15]`
- `entry_persist_min_price` in `[0, 1]` and ≤ `buy_max_price` when both are set
