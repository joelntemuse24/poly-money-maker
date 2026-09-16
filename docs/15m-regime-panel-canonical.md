# BTC 15m regime panel — CANONICAL (denser `/trades`)

Generated: `2026-09-07T05:00:27.485536+00:00` (UTC)

**Source of truth:** Data API `/trades` last-print per unix-second (~1–3s median gaps).  
CLOB 1m history remains available via `--source clob` but is **not** canonical.  
Resolution: **Chainlink TWAP** (not Binance). Binance path = regime label only.

Panel default (updated): `check_15m_regime_panel.py` → `--source trades`; **untagged** exports = main/canonical panel.

Inputs: `15m_regime_panel_trades_report.json`, `*_trades_summary.csv`, `15m_regime_panel_1m_vs_trades_comparison.json`.

---

## Primary sleeve — last **3m (180s) ≥95¢** (denser)

| Metric | Value |
|--------|------:|
| n | **2465** |
| WR | **96.51%** (2379/2465) |
| mean entry | **0.969175** |
| mean TTM @ entry | 136.4s |
| hedge-opp on losses ≤35¢ | 98.8% |
| hedge-opp on losses ≤40¢ | 100.0% |
| edge (WR − mean entry) | **-0.0041** |

Denser tape finds more/earlier first touches → lower mean entry **and** lower WR vs CLOB 1m; rank-by-WR still prefers **180s**. Hold edge slightly **negative** on denser prints (live fills worse than last-print).

---

## Compare 5m / 8m (ge95, denser)

| Window | n | WR | mean entry | edge | loss hedge ≤35¢ |
|--------|--:|---:|-----------:|-----:|----------------:|
| **3m (180)** | 2465 | 96.51% | 0.9692 | -0.0041 | 98.8% |
| 5m (300) | 2484 | 95.65% | 0.9603 | -0.0038 | 99.1% |
| 8m (480) | 2485 | 95.13% | 0.9544 | -0.0031 | 99.2% |

Rank by WR (both CLOB and trades): **180 > 300 > 480**. Recommendation vs CLOB **unchanged**.

---

## Mid band 90–94.9¢ (denser tape)

Primary window 180s: **n=1424**, WR **91.57%**, mean entry **0.9162**, edge **-0.0005**.  
(5m/8m mid: see JSON `mid_band_90_94_9_trades`.) First live probe should stay on **≥95¢**, not mid.

---

## BE / EV — primary sleeve (hourly-style framing)

- Hold **BE ≈ mean entry** = `0.969175`
- `shares = notional / entry`
- `EV ≈ shares × (1 − h) × (WR − BE)` with scratch **h=0.45** (also **h=0.35** dump)

| Notional | shares | hold EV/trade | scratch h=0.45 | scratch h=0.35 |
|---------:|-------:|--------------:|---------------:|---------------:|
| $1 | 1.0318 | -0.00419 | -0.00231 | -0.00273 |
| $2 | 2.0636 | -0.00839 | -0.00461 | -0.00545 |
| $20 | 20.6361 | -0.08386 | -0.04612 | -0.05451 |

Interpretation: denser WR < entry → hold EV slightly negative **before** fees/slippage; scratch scales magnitude toward zero but does not flip sign under BE≈entry. Keep probe tiny.

---

## Recommended live probe knobs (when Joel turns 15m on)

**DRAFT ONLY** — does **not** enable polybuybot; does **not** change `strategy_buy.json`.  
Draft file: `strategy_buy15m_probe.example.json`

| Knob | Suggestion |
|------|------------|
| window | **180s / 3.0 min** |
| min price | **≥ 0.95** |
| buy budget | **$1–2** |
| market spend cap | **~$20** |
| hedge dump | **~0.35–0.40** (`hedge_require_ask_max` ~0.40) |
| oracle | **Chainlink TWAP** (not Binance) |
| persist (vs hourly 8s) | `entry_book_persist_s≈2` from **95¢+** (`entry_persist_min_price`), `hedge_dump_persist_s≈2`, `hedge_persist_s≈1` |
| one entry / market | true |

Caveats: last-print ≠ ask; no depth; denser hold edge slightly negative — research probe only.
