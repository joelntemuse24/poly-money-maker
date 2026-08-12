# Participation autopsy (post-facto) — design

## Goal

Answer “of ~17 markets/hour, which did we buy and why might we have missed the rest?” **without changing bot logic or adding live log events.**

## Non-goals

- No edits to `buybot*.py` decision paths
- No new runtime events / rate-limited skips
- Not a millisecond latency race analyzer

## Inputs (trust both)

1. **Bot artifacts** (VM): `buybot*.log`, `underlying_research_buy*.jsonl`, optional `pnl_buy*.json`
2. **Polymarket history CSV** export (optional path)
3. **Public APIs**: Gamma events/markets, CLOB `/prices-history`

A market counts as **bought** if CSV **or** bot evidence says so. Report which source(s) matched.

## Method

For each bot cadence (5m / 15m / hourly) over `[start, end]`:

1. Enumerate markets via Gamma `events?series_slug=…` (closed + open pages).
2. Join buys by `condition_id`, slug, and normalized question/title.
3. Attach known log skip events when present (`buy_skip_*`).
4. For markets **not** bought: pull CLOB price history on Up/Down tokens over the bot’s buy window (`end − window` … `end`), classify band exposure using strategy band defaults (75–90¢) and per-bot windows (120s / 4m / 13m).

### Miss labels (price history)

| Label | Meaning |
|---|---|
| `saw_in_band` | ≥1 print in `[threshold, max_price]` during window — opportunity existed |
| `never_reached_trigger` | max print &lt; threshold |
| `above_ceiling_only` | min print &gt; max_price |
| `no_history` | API empty / failed |
| `log_skip:<event>` | named skip present (informational; may co-occur) |

Honest limit: history is sparse trade-derived prices (often ~1/min), not our exact ask snapshot.

## Output

CLI summary: bought / missed counts, miss-label histogram, optional per-market rows. Read-only.

## Delivery

Single diagnostic: `check_participation.py` (same family as `check_edge_counterfactual.py`). Document in `AGENTS.md` / `CURRENT.md`.
