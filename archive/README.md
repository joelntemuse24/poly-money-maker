# Retired paths (not live)

Live money path is **15m atomic mint** (`polymintbot` / `mintbot.py`) plus
the no-order **15m** recorder (`polypathlog` / `pathlog.py`). Buybots,
complement, hedge, DangerZone, shadow bots, and hourly-dense pathlog must
stay **off**.

This folder holds systemd units that used to start those retired programs.
Do **not** copy them back into `deploy/` or `systemctl enable` them.

| Archived unit | Program | Why it is here |
|---|---|---|
| `deploy/polybuybot.service` | `buybot.py` (15m CLOB buy) | Buy-side 15m is retired |
| `deploy/polybuybot5m.service` | `buybot5m.py` | Buy-side 5m is retired |
| `deploy/polybuybothourly.service` | `buybothourly.py` | Hourly buy/hedge is retired |
| `deploy/polycomplement.service` | `complementbot.py` | Complement lifts are retired |

Python sources for those bots stay in the repo root so tests and shared
`buy/` helpers keep working. They are not authorization to run them.

## Not in this tree (do not add or start)

- `danger_zone_index.py` / `deploy/polydangerzone.service`
- `pathlog_hourly_dense.py` / any hourly-dense unit

`pathlog.py` `SERIES` is **only** `btc-up-or-down-15m`.
