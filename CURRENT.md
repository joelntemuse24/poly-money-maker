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
- Enter when window opens within 30 minutes and is **not yet open**
- `max_open_sets`: 1 with **adjacent-window lookahead**; `sold_loser` frees the slot
- `already_minted` blocks confirmed/in-flight; `failed` remints after `mint_fail_cooldown_s` (90s) up to `mint_max_attempts` (3)
- Sells on:
  - Loser: opposite ≥ 0.90, loser ≤ 0.03 persist **5s wait** (2s in last 60s before end_ts) so wait + typical ~4s tick/FAK ≈ 9s wall (last-min ~5–6s). At fire, re-check in-range; out of range logs `sell_cancel_out_of_range` and does not POST. FAK 0.03 → 0.02
  - Winner: prefer 0.999 / redeem; allow 0.99 live-bid FAK only if loser sold ≤ 0.03 and loser+0.99 > $1; same persist + cancel-at-fire
  - Held dump: after loser sold, if held sized bid < 0.80 for **2s** → first shot is live-bid FAK. If that first shot returns no-match / kill with zero fill, immediately re-check and fast re-fire with a short descending ladder from fresh top bid toward `sell_floor` (`sell_dump_fak_retries=2`, `sell_dump_ladder_step=0.04`, `sell_dump_ladder_rungs=4` by default), stopping if the book is empty.
- Two loops: sell (`manage_sells`) and mint/discover run concurrently. Sell keeps `sell_armed_poll_s=2` while a bag is sell-hot (loser armed, or loser sold and dump/winner not done). Mint keeps `poll_s=5` and does not skip Gamma because a bag is hot. Code defaults persist 5/2/60 and dump persist 2s. **Live JSON is untouched** until the operator merges.

See `TECHNICAL_DESIGN.md` for the full guided tour.

## Deploy boundary

Copy VM → GitHub for backup. Do not blindly merge GitHub onto the VM.
Restart **only** `polymintbot` and `polypathlog` when the operator asks.
