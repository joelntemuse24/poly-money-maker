# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-12** — widened entry band / earlier windows / $2.50 budget.

---

## What we’re doing

Small live probe of three Polymarket BTC Up/Down buy bots on a GCP VM
(`~/poly-money-maker`, systemd `polybuybot` / `polybuybot5m` / `polybuybothourly`).

**Goal:** Raise *market participation* so winning fills can outpace the rare hedges.
The previous 98–99¢ / final-minutes probe missed too many markets; reversals were
hard to earn back. New posture: enter earlier/cheaper (75–90¢), accept structurally
more reversal risk, keep fail-closed book/hedge integrity.

**Not a dry run.** `dry_run: false`, `entry_enabled: true` on live strategy JSON
(gitignored). Risk is intentionally small ($2.50/market).

---

## Live probe settings (intended)

| Knob | Value | Notes |
|---|---|---|
| `buy_budget` | **$2.50** all bots | Smaller size while widening the band |
| Ask band | **0.75–0.90** | Trigger at ≥75¢; never buy above 90¢ |
| 5m window | final **120 s** (2 min) | was 90 s |
| 15m window | final **4.0 min** | was 3.0 min |
| Hourly window | final **47.0 min** | was 4.0 min — most of the hour |
| Hedge | bid ≤ **35¢**, ask ≤ **40¢**, sell-only | was 65¢ / 70¢ |
| `toxic_force_exit_below` | **0.65** | Must be ≤ `buy_threshold`; dump only if FAK avg **&lt; 65¢** |
| `min_winner_bid` / `max_loser_bid` | **0.70 / 0.30** | Required so a 75¢ ask can pass consensus/tight-book |
| `min_underlying_edge_usd` | **0.0** | Live JSON = 0; repo defaults may still say 5/10 until PR #66 |
| `underlying_gate_enabled` | **true** | Still need PTB+live, non-zero BTC direction, side match |
| `max_open_positions` | **0** | **Unlimited** (`0`); was freezing buys at 100 |

Bots hot-reload `strategy_buy.json` / `strategy_buy5m.json` / `strategy_buyhourly.json`.
Changing those files is enough for most knobs; code deploys need `git pull` (+ often
manual restart — CI “Deploy to GCP” frequently SSH-times-out).

**Operator note:** live strategy JSON is gitignored. After merging, copy the new
knobs into the VM’s live `strategy_buy*.json` (or regenerate from the `.example`
templates, preserving `dry_run: false` / `entry_enabled: true` / edge=0). Hot
reload picks them up on the next cycle — no restart required for strategy-only
edits.

---

## What we learned (Aug 11 probe)

- ~**180 fills/day** pace possible at $5; clean redeems ~**+$0.07**/fill → ~**$10/day**
  at $5, so ~**$50/market** is the rough scale for ~$100/day *at that fill rate*.
- Low reversals are driven mainly by **filling 98–99¢**, not exotic filters.
- Biggest skip pile was **`buy_skip_underlying_edge`** (the old $5/$10 move cut).
- Counterfactual (`check_edge_counterfactual.py`): edge-skipped GUI winners still
  resolved ~**95%+** (near-misses 12/12 on 5m). Resolution-only — not false-hedge PnL.
- Below-band FAK averages (e.g. gate 99¢ → fill 95¢/83¢) are “price improvement”
  walks; cannot require min fill price on a buy. Policy (old): dump only **&lt;90¢**.
- Two false hedges (~95¢ in → ~77¢ out) sold the eventual winner — hedge working as
  designed on a dip.

## Decision (Aug 12) — widen for participation

Thesis: missing markets was the binding constraint. New ask band **75–90¢**, earlier
windows (5m **2 min**, 15m **4 min**, hourly **47 min**), budget **$2.50**, hedge
arm at **35¢**. Expect more fills and likely more reversals; keep REST integrity /
underlying side-match / quarantine unchanged.

---

## Ops realities

- **VM path:** `~/poly-money-maker` on `instance-20260516-185922`.
- **Deploy:** prefer manual `git pull` + `systemctl restart polybuybot polybuybot5m polybuybothourly` when CI deploy fails.
- **Redeem:** Relayer API key path (`RELAYER_API_KEY` + address); PRECHECK_SKIPPED/zero
  positions are blacklisted so they don’t 429-spam.
- **Diagnostics:** `check_book.py`, `check_edge_counterfactual.py` (read-only).
- **Secrets:** were pasted in chat historically — rotate if not already done. Never commit `.env`.

---

## Incidents

- **2026-08-12 ~4h no 5m buys:** UI showed `POS 100` with `max_open_positions=100`.
  Cap counted Data-API sizes (incl. redeemable) → silent skip of every entry.
  **Decision: disable the cap** (`max_open_positions: 0` = unlimited). Code also
  ignores redeemable when a positive cap is set, and logs `buy_skip_max_positions`.

## Open / next

- [x] Max-open freeze fixed + deployed (`d659693`); live `max_open_positions=0`.
- [ ] Apply widened 75–90¢ / $2.50 / new windows / 35¢ hedge to **live** strategy JSON on the VM.
- [ ] Watch fill count vs hedge rate under the wider band (expect more of both).
- [ ] Merge PR #66 so *repo defaults* also use `min_underlying_edge_usd: 0` (live JSON already 0).
- [ ] Revisit size once participation and hedge rate look acceptable.
- [ ] Optional later: retry empty FAKs in-window; redeem backlog.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing buy/hedge/redeem logic.
2. Propagate logic fixes across `buybot.py` / `buybot5m.py` / `buybothourly.py`.
3. Never truncate state/PnL/log files; never commit live strategy/state/`.env`.
4. When an ops decision changes, **update this file in the same PR/commit**.
