# Agent guidance — Poly Money Maker

## Operational truth

The live VM is the source of truth. This repository snapshot was aligned
to the VM on 2026-09-19. Historical buy/hedge documents are gone from
this tree; they are not current operational instructions.

**Live services:** `polymintbot` (`mintbot.py` + `strategy_mint.json`) and
`polypathlog` (`pathlog.py`, **15m only**). Atomic mint on
`btc-up-or-down-15m`. Buybots, complementbot, hedge, DangerZone, shadow
bots, and hourly-dense pathlog stay **off**. Do not start them, and do
not add those sources back. `scrapbidder.py` /
`deploy/polyscrapbid.service` is an opt-in bids-only sister process
(wallet B). It is not a restore of `complementbot.py`. Leave it stopped
until the operator asks.

Never infer service state from filenames or old documentation. Read
current configuration and read-only service status before operational
work.

## Safety boundaries

- Never read or commit .env files, credentials, private keys, or API secrets.
- Never start, stop, restart or signal live services without explicit operator authorization.
- Optional Cloud Agent SSH: when runtime secret `POLY_VM_SSH_KEY` is set, `ssh poly-vm` is read-only as `poly-auditor` (logs, strategy JSON, check scripts). See CLOUD_RESEARCH.md ("Cursor Cloud → poly-vm SSH"). Still never read `.env` or place live orders.
- Do not import `mintbot.py` in tests: module initialization loads credentials, acquires a process lock and creates clients. Use AST-extracted functions with stubs, or the pure `buy/` helpers mint/pathlog actually import.
- Perform development and tests in an isolated clone. Never install into the live virtualenv or write live runtime state.
- Preserve confirmed order identity, durable uncertain-order state, exact financial evidence, and expiry checks. A matched status or a transient zero balance alone is not proof of settled execution.

## Code and validation

`mintbot.py` is the live entry point. It mints complete sets on
**btc-up-or-down-15m** only (`strategy_mint.example.json` mirrors the
template: dry_run=true, entry_enabled=false, sell_enabled=false). Optional
sell stays off until live `strategy_mint.json` sets `sell_enabled=true`.
Loser scrap: sized opposite bid ≥ `sell_opposite_min` (~0.90), loser ≤
`sell_threshold` (0.04) **arms**. 4¢ is the ceiling to start hunting, not
the print. Persist `sell_persist_s` (~2.5s), or `sell_persist_last_min_s`
(~1s) when time-to-end is within `sell_persist_last_min_window_s` (~60s).
Skip that wait when TTM ≤ `sell_persist_skip_ttm_s` (~90s) or when bid
depth at the FAK rung covers our size. Then re-check in-range at fire and
FAK `sell_fak_px` (0.03) → `sell_floor` (0.02), or the live bid when the
book is thinner than the print. Do not post a 4¢ sell. Empty FAK or a
vanished loser book after arm keeps `armed_ts`. On
`empty_keep_arm` / `empty_fak_keep_arm`, fire a blind 1¢ FAK
(`sell_scrap_blind_px`, backoff `sell_scrap_blind_backoff_s` ~3s). After
the first FAK miss while still armed, rest a GTD/GTC sell at
`sell_scrap_rest_px` (0.03, the print). Cancel that rest on fill, window
end, loser no longer qualifies, or a hard late-window oracle block. An
empty book alone does not pull a rest that still has edge. Out of range
at fire logs `sell_cancel_out_of_range` and does not POST. **Late-window
oracle veto:** when TTM ≤ `sell_late_window_s` (~120), also require
side-aware Chainlink TWAP edge ≥
`max(sell_oracle_edge_floor_usd, sell_oracle_edge_per_ttm × TTM)` for
`sell_oracle_edge_persist_s` (~3s) before any new loser post (FAK, blind,
or rest), fail-closed on missing/stale tape (`sell_oracle_stale_s` ~5);
log `sell_loser_oracle_block` / `sell_loser_oracle_ok`. Skip-persist does
not bypass that veto. Outside that window, CLOB gates only.
Winner cash-out is a separate path at `sell_winner_min` (~0.999).
Live-bid FAK the winner, then clamp `limit = min(live_sized_bid,
sell_clob_max_price=0.99)` (floor `sell_clob_min_price=0.01`) so rich
0.995–0.999 books fill; log `sell_winner_limit_clamped` when live >
posted. Do not import `mintbot.py` in tests.

`pathlog.py` records public CLOB books for **btc-up-or-down-15m** only.
Keep `deploy/polypathlog.service`. Do not start `pathlog_hourly_dense.py`
or add 5m/hourly back to `SERIES`.

`mintbot.py` runs sell and mint as independent loops so Gamma/relayer
work cannot steal a dump tick. Do not re-serialize them into one
`manage_sells → discover → sleep` cycle. Persist / last-min / window
defaults are 2.5/1/60 (dump persist stays 2s); `sell_armed_poll_s` is
sell-loop cadence only. Live `strategy_mint.json` still wins for keys it
already sets (`sell_threshold`, `sell_persist_s`, `sell_persist_last_min_s`
were 0.03 / 5 / 2 on the 19 Sep snapshot). New keys absent from that file
(`sell_fak_px`, skip-persist, blind, rest) take these defaults after the
operator pulls and restarts. Do not edit live JSON from this repo.

Wallet A (mintbot) never posts a bid. There is no same-wallet buyback.
`scrapbidder.py` is a separate process for wallet B. Cap is 20 shares
and `bid_max_px` 0.04 (~$0.80). After A `sold_loser` on leg L, B buys L
at the live price: FAK the ask when it is ≤ the cap (`bid_take_enabled`,
default true), otherwise join a live bid at or under `bid_rest_px` (3¢),
or chase a recovering bid that is still ≤ the cap. An empty or richer
book rests at 3¢, not at 4¢. A's `sell_scrap_rest_id` does not
block that leg, and the last-`active_ttm_s` (~180s) window does not apply
to that post-scrap hedge. Still cancel by T−`cancel_ttm_s` (~20s). The
winner leg A still holds stays blocked. Markets A never held use that
same quote on the cheap live side only inside the last 180s. If sold_loser on L
has no B bid or fill and the window is still open past cancel, log
`scrapbid_miss` (condition, leg, ttm, age) after ~10s, throttled ~30s.
Poll drops to `poll_hot_s` (~1s) while that gap is open. Each pass
re-reads mint intents after quoting, and quotes only open sister orders,
the late `active_ttm_s` window, or a sold leg — not every future market.
It never mints and never FAK-sells. `bid_enabled` defaults false and
`dry_run` defaults true.
Credentials stay in gitignored `.env.complement` (POLY_1271 / deposit
wallet). Refuse the mintbot funder. Do not put `.env.complement` values
in git. `deploy/polyscrapbid.service` stays off until the operator asks.

Shared `buy/` helpers exist for mint, pathlog, and the recording-only oracle tape:

- `buy/book.py` — CLOB top-of-book parsing (`pathlog`, mint sized bids)
- `buy/mint_sell.py` — sell fill parse, inventory latch, arm/persist
- `buy/mint_loops.py` — concurrent sell vs mint job runner + intent claim
- `buy/market.py` — Gamma/CLOB discovery (`mintbot`, `pathlog`)
- `buy/chain.py` — Polygon eth_call prechecks (`mintbot`)
- `buy/contracts.py` — atomic mint calldata (`mintbot`)
- `buy/oracle_log.py` — Chainlink BTC/USD 60s TWAP tape
  (`logs/oracle_twap.jsonl`) plus read-only `bag_view` for the late
  loser-scrap veto. Not an input to mint, winner, dump, or sell outside
  `sell_late_window_s`.
- `buy/sister_bid.py` — wallet B buy policy. Post-scrap FAKs a live ask
  at or under `bid_max_px`, else joins a bid at or under `bid_rest_px`,
  else chases a recovering bid still under the cap, else rests at 3¢.
  A's rest and the 180s window do not block that leg; cancel near
  expiry. `scrapbidder.py` posts the orders. Not an input to mint.

Do not restore retired buybot modules (`entry_skip`, `hedge_gate`,
`btc_price`, `clob_book_ws`, `depth_ladder`, `strategy_coherence`,
`entry_rest_gtd`, complementbot, probe/journal helpers).

Run tests with `python -m unittest discover -s tests -p 'test_*.py' -v`
in a disposable sandbox. Keep temporary files and Python caches in that
sandbox. Live `strategy_mint.json` is gitignored; the example is not
authorization to launch the bot.

## Repository policy

Do not add runtime configs, logs, backups, lock files, positions, P&L
state, journals, wallet exports, or virtualenvs. Knob JSON already
tracked in git (`strategy_mint.example.json`) is fine. Live mint/buy
knob files stay gitignored.

The existing main-branch deployment workflow pulls code and installs
dependencies on the VM after merge. It does not restart services.
Creating a PR is not authorization to merge or deploy it. After a pull,
only `polymintbot` and `polypathlog` may be restarted, and only when the
operator asks. `polyscrapbid` stays stopped until the operator asks to
start it.
