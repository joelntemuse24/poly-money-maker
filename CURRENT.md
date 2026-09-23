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

## Repo code defaults (23 September 2026)

Scrap knobs are back to the pre-2026-09-22 rules in `strategy_mint.example.json` and `mintbot` `DEFAULTS`:

- `sell_threshold` **0.03**. Print stays FAK `sell_fak_px` **0.03** then `sell_floor` **0.02**, or the live bid when the book is thinner.
- `sell_persist_s` **5**, `sell_persist_last_min_s` **2** (window still 60s). Dump persist stays 2s.
- `sell_persist_skip_when_sized` **false**. A sized book waits the full persist. TTM ≤ `sell_persist_skip_ttm_s` (90s) still skips.
- The sister-miss held dump is gone. No `sell_dump_if_sister_miss_s`. The bid-under-`sell_dump_below` (0.80) persist dump is unchanged. A filled normal dump sets `sell_dump_leg` (the leg A sold). Inventory already flat does not.
- Wallet A never posts a bid. Same-wallet buyback is not implemented.

`load_strategy` overlays only keys already present in live `strategy_mint.json`. If that file still has `sell_threshold` 0.05, persist 2.5/1, or `sell_persist_skip_when_sized` true from the 22 Sep edit, those live values win until the operator patches the VM file. A leftover `sell_dump_if_sister_miss_s` key is ignored because it is no longer in `DEFAULTS`. Do not edit the live file from git.

## Sister scrap bidder (wallet B, opt-in, off)

`scrapbidder.py` + `deploy/polyscrapbid.service` buy from the complement deposit wallet. **20 shares**, rest at **`bid_rest_px` 5¢**. After A `sold_loser` on leg L, B FAK-buys 20 shares at the **live ask** whenever that ask is ≤ **7.5¢** (`bid_fak_max_notional / shares` = 1.50/20). If `shares * ask` is under **$1**, the FAK **limit** is raised to **5¢** (`1/shares`) so the order notional clears Polymarket's marketable-BUY floor; the fill is still the cheaper ask. FAK only when limit × shares is in **[$1.00, $1.50]**. If that take cannot fire, B rests a GTD/GTC BUY at 5¢ only when the rest is strictly below the ask (or the ask is missing). A rest at or above the ask is skipped (`cross_ask`). A's scrap rest and the 180s window do not block that leg. There is no price ladder. GTD is used when the expiration is at least ~180s ahead; otherwise the order is GTC and still cancelled by T−20s / window end. The winner leg A still holds stays blocked. Markets A never held are not bid (`bid_absent_enabled` defaults false).

**Dump hedge:** when A sets `sell_dump_leg` (normal held dump only), B FAK-buys **`dump_hedge_shares` (10)** of the **other** leg. Same notional band via `dump_hedge_fak_min_notional` / `dump_hedge_fak_max_notional` (default $1.00–$1.50 → 10¢–15¢ at 10 shares). Otherwise rest at `dump_hedge_rest_px` (10¢) when that rest does not cross the ask. Fills live in `dump_filled`, separate from the 20-share scrap clip. Raise the max notional if the other side is richer than 15¢.

**A→B top-up:** when a hedge place is wanted and B's on-chain pUSD is under `topup_need_usd` ($1.50), or the place fails balance/allowance, scrapbidder spawns `sister_topup.py --once --live`. That script reads mintbot `.env` (not `.env.complement`) and submits one gasless PROXY batch: pUSD `transfer` of `topup_usd` ($5) from A's funder to deposit wallet `0x2b2D1dA1a49E8BF73EbBC3EAC35D79cc88cd4ad2`. It refuses the Magic proxy `0xCfF5…` and refuses to sign as B. One accepted transfer per broke episode (`positions_topup.json`, gitignored). The episode closes when B's balance is back above the need, or when a place-fail that happened while B already looked funded is followed by a successful place. Scrapbidder calls `update_balance_allowance` (collateral) before a live place so the credited pUSD is the CLOB balance. `--live` still no-ops while strategy `dry_run` is true or `topup_enabled` is false. Do not add `.env` to `polyscrapbid.service`.

Manual check (no orders): `.venv/bin/python sister_topup.py --once --reason manual`. A real submit also needs `--live` and `dry_run` false. There is no second systemd unit.

If that sold leg has no B bid or fill for ~10s while the window is open past cancel, log `scrapbid_miss` (throttled ~30s) and poll at ~1s until the order is up. Books are quoted only for open sister orders, the last 180s, a sold leg, or a dump hedge, and mint intents are read again after that quote so a scrap during the pass is not planned as `a_still_long`. Never mint. Never FAK-sell. `bid_enabled` false and `dry_run` true until the operator turns them on. Credentials stay in gitignored `.env.complement`. Do not start `polyscrapbid` unless the operator asks. This is not `complementbot`.

See `TECHNICAL_DESIGN.md` for the full guided tour.

## Chainlink TWAP tape (+ late loser-scrap veto)

`oracle_log_enabled` defaults **on**. A missing key in live `strategy_mint.json` stays on; do not edit that file for this tape. While a 15m mint intent is open (including the pre-open bag and ~2 minutes after the end), mintbot appends `logs/oracle_twap.jsonl`.

The live path is Polymarket RTDS topic `crypto_prices_twap_sixty` for `btc/usd` (Chainlink's 60s TWAP, no Data Streams credentials). Window price-to-beat and the completed close come from `GET /api/crypto/crypto-price?symbol=btc&variant=fifteen&eventStartTime=<window start>`. `variant=fifteen` is the 15m Chainlink series; other variant strings fall back to hourly Binance and are not used.

Stored samples are 15s through the middle of the window, 2s around the open and in the last 3 minutes, and 1s in the last 60s and just after the end. The recorder wakes every second while a bag is open so that tighter cadence is not stuck behind a cold sleep. A dead feed appends `oracle_log_fail` and logs the same event.

**Trading use is limited to the late loser-scrap gate:** when TTM ≤ `sell_late_window_s` (120), mintbot reads the live feed + `open_ref` already tracked for the bag (no second websocket) and blocks full loser scrap unless the side-aware edge holds for `sell_oracle_edge_persist_s` (3). Mint eligibility, winner cash-out, and held dump do **not** trade on the oracle. Outside the late window the tape is audit-only.

## Deploy boundary

Copy VM → GitHub for backup. Do not blindly merge GitHub onto the VM.
Restart **only** `polymintbot` and `polypathlog` when the operator asks.
