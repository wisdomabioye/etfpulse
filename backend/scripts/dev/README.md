# scripts/dev — verification + smoke utilities

One-off probes and verification scripts. **Not** runtime tools — they exist to
verify external API contracts, capture response shapes, and smoke-test the
pipeline against real services. None of these are referenced by Coolify, CI,
or the application itself.

Operational scripts (intended for production / demo use) live one level up
in `scripts/` — currently `backfill_signal_prices.py` and `seed_demo.py`.

## What's here

| Script | Purpose | API cost |
|---|---|---|
| `verify_sosovalue.py` | Hits each SoSoValue endpoint once, prints parsed DTO + raw shape. Confirms adapter parsing matches live response. | ~6 calls |
| `verify_sector_spotlight.py` | Captures `/currencies/sector-spotlight` body to a fixture file. | 1 call |
| `verify_fixtures_parse.py` | Loads every `sosovalue_*.json` fixture through the adapter (offline). Confirms refreshed fixtures still parse. | 0 |
| `capture_fixtures.py` | Refreshes all 9 SoSoValue fixtures from live API. **Will break pinned test assertions** — see CLAUDE.md fixture refresh notes. | 9 calls |
| `probe_sector_spotlight.py` | Hard verification of the sector-spotlight endpoint contract: param decorativeness, auth, field types, body equality across calls. | 5 calls |
| `probe_rate_limit_429.py` | Bursts ~35 parallel calls to confirm per-minute 429 wording. Used to verify `_classify_and_raise_429` substring routing on the upgraded tier. | up to 35 calls |
| `smoke_e2e.py` | End-to-end smoke against real Postgres + fixture-mode adapters: ingest → detectors → signal builder → regime → outcome eval. Prints persisted state and exercises every Stage 06+ read path. | 0 (fixture mode) |

## Running

All assume `cwd = etfpulse/backend/`:

```bash
SOSOVALUE_USE_FIXTURES=false uv run python scripts/dev/verify_sosovalue.py
SOSOVALUE_USE_FIXTURES=true  uv run python scripts/dev/smoke_e2e.py
```

## When to use

- Before relying on a SoSoValue endpoint shape that hasn't been verified live recently → `verify_sosovalue.py` or one of the targeted probes.
- After a quota reset, before assuming the rate-limit classifier is correct → `probe_rate_limit_429.py`.
- When investigating a parsing regression → `verify_fixtures_parse.py` (offline, fast).
- For a sanity check that the daily pipeline still wires end-to-end after a refactor → `smoke_e2e.py`.

## Don't

- Don't run `capture_fixtures.py` casually. Refreshing fixtures means walking ~5+ pinned test assertions in `tests/test_adapters/test_sosovalue.py` (and likely downstream tests) and updating their values.
- Don't add probe scripts that live-call paid APIs without a per-script "API cost" line in this table.
