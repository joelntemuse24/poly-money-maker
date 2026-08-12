# CURRENT.md — Live probe status

**Agents: read this after `AGENTS.md`.** Update this file when ops/strategy decisions change.
Do not put secrets, API keys, or live wallet material here.

Last updated: **2026-08-12** — buy bots stopped; mint-only helper for manual sells.

---

## What we’re doing

**Automated CLOB buy/hedge probe is paused.** All three buy services are
**stopped + disabled** on the VM (`polybuybot` / `polybuybot5m` /
`polybuybothourly`). Operator will **sell manually**.

**Active helper:** `mintbot.py` (`polymintbot`) — mint complete sets (default
**6 Up + 6 Down** = **$6** pUSD) on BTC Up/Down 5m/15m/hourly markets with
**0 < TTM ≤ 70 min**, if collateral allows. **No CLOB buys. No auto-hedges.**

Thesis unchanged (trade the decided leg / cut obvious reversals by hand); automation
was not catching enough markets or hedges reliably.

---

## Mint bot settings (intended)

| Knob | Value | Notes |
|---|---|---|
| `shares` | **6.0** | $6 collateral → 6 Up + 6 Down |
| `enter_max_ttm_min` | **70** | only markets ending within 70 minutes |
| Series | 5m + 15m + hourly | same Gamma series as buy bots |
| `dry_run` | start **true** | set false only when ready |
| `entry_enabled` | start **false** | must be true to mint (incl. dry) |
| `max_open_sets` | 40 | safety cap |
| `max_daily_notional` | 500 | ~83 mints/day cap |

Live file: `strategy_mint.json` (gitignored). Template: `strategy_mint.example.json`.

---

## Ops

- **VM:** `~/poly-money-maker` on `instance-20260516-185922`.
- **Buy bots:** keep disabled unless explicitly re-enabled.
- **Mint bot:**
  ```bash
  cp strategy_mint.example.json strategy_mint.json
  # edit: entry_enabled true for dry; later dry_run false for live
  python mintbot.py
  # or: sudo systemctl enable --now polymintbot   # after unit installed
  ```
- **Kill switch:** `touch STOP_MINT` in the repo root.
- **Secrets:** `.env` (PRIVATE_KEY, FUNDER_ADDRESS, RELAYER_* or POLY_BUILDER_*).

---

## Open / next

- [x] Stop + disable buy bot systemd units.
- [ ] Deploy mintbot: `strategy_mint.json`, dry-run verify, then live mint.
- [ ] Manual sell workflow on Polymarket UI.
- [ ] Revisit automation only if mint inventory + manual sells prove the thesis.

---

## Agent instructions

1. Read `AGENTS.md` + this file before changing mint/buy/hedge logic.
2. Do **not** re-enable buy bots unless the operator asks.
3. Mint path must not place CLOB buys or sells.
4. Never truncate state/PnL/log files; never commit live strategy/state/`.env`.
5. When an ops decision changes, **update this file in the same PR/commit**.
