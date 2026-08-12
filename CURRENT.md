# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-12** (~01:42 UTC) — VM on `d659693`, bots restarted.

---

## What we’re doing

Small live probe of three Polymarket BTC Up/Down buy bots on a GCP VM
(`~/poly-money-maker`, systemd `polybuybot` / `polybuybot5m` / `polybuybothourly`).

**Goal:** Maximize *safe* fills at tiny size, measure hit rate / false hedges /
below-band walks, then raise `buy_budget` toward ~**$100/day** profit without
needing ~$50/market at today’s thin fill rate.

**Not a dry run.** `dry_run: false`, `entry_enabled: true` on live strategy JSON
(gitignored). Risk is intentionally small ($5/market).

---

## Live probe settings (intended)

| Knob | Value | Notes |
|---|---|---|
| `buy_budget` | **$5** all bots | Scale later after fill-rate OK |
| Ask band | **0.98–0.99** | Gate is top-of-book before send |
| 5m window | final **90s** | |
| 15m window | final **3.0 min** | |
| Hourly window | final **4.0 min** | Thin participation historically |
| Hedge | bid ≤ **65¢**, sell-only | False hedges accepted as cost of safety |
| `toxic_force_exit_below` | **0.90** | Force-dump only if FAK avg **&lt; 90¢**; 90–98¢ uses normal hedge |
| `min_underlying_edge_usd` | **0.0** | Live JSON = 0; repo defaults may still say 5/10 until PR #66 |
| `underlying_gate_enabled` | **true** | Still need PTB+live, non-zero BTC direction, side match |
| `max_open_positions` | **0** | **Unlimited** (`0`); was freezing buys at 100 |

Bots hot-reload `strategy_buy.json` / `strategy_buy5m.json` / `strategy_buyhourly.json`.
Changing those files is enough for most knobs; code deploys need `git pull` (+ often
manual restart — CI “Deploy to GCP” frequently SSH-times-out).

---

## What we learned (Aug 11 probe)

- ~**180 fills/day** pace possible at $5; clean redeems ~**+$0.07**/fill → ~**$10/day**
  at $5, so ~**$50/market** is the rough scale for ~$100/day *at that fill rate*.
- Low reversals are driven mainly by **filling 98–99¢**, not exotic filters.
- Biggest skip pile was **`buy_skip_underlying_edge`** (the old $5/$10 move cut).
- Counterfactual (`check_edge_counterfactual.py`): edge-skipped GUI winners still
  resolved ~**95%+** (near-misses 12/12 on 5m). Resolution-only — not false-hedge PnL.
- Below-band FAK averages (e.g. gate 99¢ → fill 95¢/83¢) are “price improvement”
  walks; cannot require min fill price on a buy. Policy: dump only **&lt;90¢**.
- Two false hedges (~95¢ in → ~77¢ out) sold the eventual winner — hedge working as
  designed on a dip.

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
- [ ] Confirm 5m buys resume after the cap unblock (watch `buy_attempt` / `buy_success`).
- [ ] Merge PR #66 so *repo defaults* also use `min_underlying_edge_usd: 0` (live JSON already 0).
- [ ] Watch fill count vs hedge rate for a few days at edge=$0 + uncapped opens.
- [ ] Then reconsider size ($20–25/market) once fill density supports ~$100/day with margin.
- [ ] Optional later: retry empty FAKs in-window; hourly under-participation; redeem backlog.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing buy/hedge/redeem logic.
2. Propagate logic fixes across `buybot.py` / `buybot5m.py` / `buybothourly.py`.
3. Never truncate state/PnL/log files; never commit live strategy/state/`.env`.
4. When an ops decision changes, **update this file in the same PR/commit**.
