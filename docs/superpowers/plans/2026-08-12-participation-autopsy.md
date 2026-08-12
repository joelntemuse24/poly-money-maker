# Participation autopsy — implementation plan

## Files

- **Create** `check_participation.py` — read-only CLI
- **Update** `AGENTS.md` — list under Safe to modify / diagnostics
- **Update** `CURRENT.md` — note tool under Diagnostics / Open

## Tasks

1. Implement market enumeration (Gamma series pages) + buy loaders (CSV, logs, research, pnl).
2. Implement price-history band classification + skip attachment.
3. CLI: `--hours` / `--start-ts` / `--end-ts`, `--bot`, `--csv`, `--band`, window overrides.
4. Smoke-test against live Gamma/CLOB for last ~2h (5m at least).
5. Commit, push, open PR.
