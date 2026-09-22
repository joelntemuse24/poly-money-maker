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
- `submitting` intents without a relayer `transaction_id` auto-fail after `mint_submitting_timeout_s` (90s by default, 0 disables) so restart ghosts cannot pin `wait_submit`
- Sells on:
  - Loser: opposite ≥ 0.90, loser ≤ 0.03 persist **5s wait** (2s in last 60s before end_ts) so wait + typical ~4s tick/FAK ≈ 9s wall (last-min ~5–6s). At fire, re-check in-range; out of range logs `sell_cancel_out_of_range` and does not POST. FAK 0.03 → 0.02. **Late-window oracle veto (≤120s TTM):** side-aware Chainlink TWAP edge vs window open must stay ≥ `max(25, 1.5 × TTM_s)` for 3s continuous (fail-closed if tape missing/stale); logs `sell_loser_oracle_block` / `sell_loser_oracle_ok`. Combat for true reverse `btc-updown-15m-1790078400` (TTM≈42 needed ≳$63, edge ~+$20 → block). Outside 120s, CLOB gates only.
  - Winner: prefer 0.999 / redeem; allow 0.99 live-bid FAK only if loser sold ≤ 0.03 and loser+0.99 > $1; same persist + cancel-at-fire
  - Held dump: after loser sold, if held sized bid < 0.80 for **2s** → first shot is live-bid FAK. If that first shot returns no-match / kill with zero fill, immediately re-check and fast re-fire with a short descending ladder from fresh top bid toward `sell_floor` (`sell_dump_fak_retries=2`, `sell_dump_ladder_step=0.04`, `sell_dump_ladder_rungs=4` by default), stopping if the book is empty.
- Two loops: sell (`manage_sells`) and mint/discover run concurrently. Sell keeps `sell_armed_poll_s=2` while a bag is sell-hot (loser armed, or loser sold and dump/winner not done). Mint keeps `poll_s=5` and does not skip Gamma because a bag is hot. The 19 Sep live file still has persist 5/2 and dump persist 2s. **That live JSON is untouched** until the operator edits it.

## Repo code defaults (22 September 2026 — not live until pull + restart)

`strategy_mint.example.json` and `mintbot` `DEFAULTS` now arm loser scrap at **4¢** (`sell_threshold=0.04`). The sell print is **not** 4¢: FAK `sell_fak_px` **3¢** then `sell_floor` **2¢**, or the live bid when the book is thinner. A 4¢ book still posts 3¢ → 2¢.

- Persist wait is half: `sell_persist_s` **2.5s**, `sell_persist_last_min_s` **1s** (window still 60s). Dump persist stays 2s.
- Skip persist when TTM ≤ `sell_persist_skip_ttm_s` (90s), or when depth at the FAK rung covers our size.
- Empty keep: blind FAK at 1¢ with a 3s backoff.
- After a FAK miss while still armed: resting GTD/GTC sell at `sell_scrap_rest_px` **3¢** (the print). Set that knob to 0.01 or 0.02 if a phantom 1–2¢ book should be caught instead. Cancel on fill, window end, disqualify, or a hard oracle block.
- Late-window oracle veto is unchanged (TTM ≤ 120s, side-aware TWAP, fail-closed). Skip-persist does not bypass it.
- Winner cheap 0.99 still requires a recorded loser fill ≤ 0.03. Arming at 4¢ does not open that gate by itself.
- Wallet A never posts a bid. Same-wallet buyback is not implemented.

`load_strategy` overlays only keys already present in live `strategy_mint.json`. After the operator pulls this code and restarts `polymintbot`, missing keys (fak price, skip, blind, rest) take the defaults above. Keys the live file already sets (`sell_threshold` 0.03, `sell_persist_s` 5, `sell_persist_last_min_s` 2 on the 19 Sep snapshot) stay until the operator edits those keys. Do not edit the live file from git.

## Sister scrap bidder (wallet B, opt-in, off)

`scrapbidder.py` + `deploy/polyscrapbid.service` buy from the complement deposit wallet. Cap is **20 shares** and **`bid_max_px` 4¢** (~$0.80), not a sell and not a price we always pay. After A `sold_loser` on leg L, B buys L immediately (A's scrap rest and the 180s window do not block it): FAK the live ask when it is ≤ 4¢ (`bid_take_enabled` true), else join a live bid at or under `bid_rest_px` 3¢, else chase a recovering bid that is still ≤ 4¢, else rest at 3¢ when the book is empty or richer. Never rest or pay above 4¢. Cancel by T−20s / window end. The other leg (winner A still holds) stays blocked. Markets A never held still wait for the last 180s and a cheap book, then use that same quote. If that sold leg has no B bid or fill for ~10s while the window is open past cancel, log `scrapbid_miss` (throttled ~30s) and poll at ~1s until the order is up. Books are quoted only for open sister orders, the last 180s, or a sold leg, and mint intents are read again after that quote so a scrap during the pass is not planned as `a_still_long`. Never mint. Never FAK-sell. `bid_enabled` false and `dry_run` true until the operator turns them on. Credentials stay in gitignored `.env.complement`. Do not start `polyscrapbid` unless the operator asks. This is not `complementbot`.

See `TECHNICAL_DESIGN.md` for the full guided tour.

## Chainlink TWAP tape (+ late loser-scrap veto)

`oracle_log_enabled` defaults **on**. A missing key in live `strategy_mint.json` stays on; do not edit that file for this tape. While a 15m mint intent is open (including the pre-open bag and ~2 minutes after the end), mintbot appends `logs/oracle_twap.jsonl`.

The live path is Polymarket RTDS topic `crypto_prices_twap_sixty` for `btc/usd` (Chainlink's 60s TWAP, no Data Streams credentials). Window price-to-beat and the completed close come from `GET /api/crypto/crypto-price?symbol=btc&variant=fifteen&eventStartTime=<window start>`. `variant=fifteen` is the 15m Chainlink series; other variant strings fall back to hourly Binance and are not used.

Stored samples are 15s through the middle of the window, 2s around the open and in the last 3 minutes, and 1s in the last 60s and just after the end. The recorder wakes every second while a bag is open so that tighter cadence is not stuck behind a cold sleep. A dead feed appends `oracle_log_fail` and logs the same event.

**Trading use is limited to the late loser-scrap gate:** when TTM ≤ `sell_late_window_s` (120), mintbot reads the live feed + `open_ref` already tracked for the bag (no second websocket) and blocks full loser scrap unless the side-aware edge holds for `sell_oracle_edge_persist_s` (3). Mint eligibility, winner cash-out, and held dump do **not** trade on the oracle. Outside the late window the tape is audit-only.

## Deploy boundary

Copy VM → GitHub for backup. Do not blindly merge GitHub onto the VM.
Restart **only** `polymintbot` and `polypathlog` when the operator asks.
