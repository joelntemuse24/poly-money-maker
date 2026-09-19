# Operational snapshot — 19 September 2026

Source: live Google VM (`/home/ntemusejoel/poly-money-maker`). VM is the
source of truth.

**Live money path:** atomic mint on **15m only** (`polymintbot` /
`mintbot.py` + gitignored `strategy_mint.json`). **Live recorder:**
`polypathlog` / `pathlog.py` with series `btc-up-or-down-15m` only.

Buybots, complement, hedge, DangerZone, shadow bots, and hourly-dense
pathlog are **stopped / retired**. Do not start them.

## Live mint knobs (gitignored `strategy_mint.json`)

- Series: `btc-up-or-down-15m` only
- `shares`: 5 ($5 trial complete set)
- `entry_enabled`: true · `dry_run`: false
- Enter when window opens within 16 minutes and is **not yet open**
- `max_open_sets`: 1 with **adjacent-window lookahead**; `sold_loser` frees the slot
- `already_minted` includes `failed` (no remint storm)
- Sells on:
  - Loser: opposite ≥ 0.90, loser ≤ 0.03 persist 5s, FAK 0.03 → 0.02
  - Winner: prefer 0.999 / redeem; allow 0.99 live-bid FAK only if loser sold ≤ 0.03 and loser+0.99 > $1
  - Held dump: after loser sold, if held sized bid < 0.80 for 5s → live-bid FAK

See `TECHNICAL_DESIGN.md` for the full guided tour.

## Deploy boundary

Copy VM → GitHub for backup. Do not blindly merge GitHub onto the VM.
Restart **only** `polymintbot` and `polypathlog` when the operator asks.
