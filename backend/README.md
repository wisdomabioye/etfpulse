# ETFPulse backend

Python 3.12, managed by **uv**. FastAPI + async SQLAlchemy 2.x + asyncpg + Alembic. APScheduler runs in-process. `python-telegram-bot` runs as a background task inside FastAPI's lifespan — single process, single deploy. No Redis, no Celery — `cachetools` for TTL caches.

For the full architecture reference (conventions, anti-drift rules, env-var policy, deploy flow), read `../../CLAUDE.md`. This file is the orientation cheatsheet.

## Package map (high level)

```
etfpulse/
  app.py         create_app() factory + module-level `app` for uvicorn
  config.py      pydantic-settings — all tunables, env-driven
  db.py          async engine + session + FastAPI dependency
  models/        multi-user schema (users, channels, groups, signals, deliveries, …)
  adapters/      external services — sosovalue, openrouter, telegram, binance
  pipeline/      ingestion, detectors, signal builder, delivery, regime, track record, reapers
  api/           every HTTP/FastAPI concern — routes, schemas, deps, exceptions, lifespan, config_check
  bot/           Telegram command + callback handlers + i18n + keyboards
migrations/      Alembic (async env.py)
fixtures/        Captured SoSoValue + Binance JSON for offline tests
scripts/         operational scripts (backfill_signal_prices.py, seed_demo.py)
scripts/dev/     verification + probe utilities — see scripts/dev/README.md
tests/           pytest + asyncio + httpx_mock; uses migrations against etfpulse_test
```

## Quick start

```bash
# Stock local Postgres with postgres:postgres creds
createdb -U postgres etfpulse
createdb -U postgres etfpulse_test

cp .env.example .env            # fill in SOSOVALUE_API_KEY, OPENROUTER_API_KEY, ADMIN_API_KEY
uv sync --extra dev
uv run poe migrate
uv run poe dev                  # uvicorn --reload on :8000
```

Telegram bot is disabled until all four `TELEGRAM_*` env vars are populated — `is_bot_enabled` is the single gate. Useful for dev without a bot account.

## Poe tasks

```bash
uv run poe dev                  # uvicorn --reload
uv run poe test                 # pytest against etfpulse_test
uv run poe check                # lint + format + typecheck + tests (matches CI)
uv run poe migrate              # alembic upgrade head
uv run poe revision "msg"       # autogenerate a migration + ruff format/check it
uv run poe migrate-check        # AST scan — every migration has a non-trivial downgrade()
uv run poe migrate-roundtrip    # DB round-trip: upgrade → downgrade -1 → upgrade head
```

Single-command equivalents:

```bash
uv run pytest tests/test_pipeline/test_signal_builder.py::test_x   # one test
uv run pytest -k "pattern"                                          # filter by name
uv run ruff format .                                                # apply formatting
```

## Conventions worth knowing before editing

- **All routes** live in `etfpulse/api/routes/` and register via `ALL_ROUTERS` — `app.py` never calls `include_router` directly.
- **All startup/shutdown logic** is a `StartupTask` in `etfpulse/api/lifespan.py`. No `@app.on_event(...)`.
- **All detectors** live in `etfpulse/pipeline/detectors/*.py` and register in `ALL_DETECTORS`. New detector = new file + append to registry.
- **All bot commands** live in `etfpulse/bot/handlers/*.py` and register via `register_handlers(application)`.
- **Pipeline functions don't commit** (D14). The caller (scheduler wrapper or admin route) owns the transaction boundary.
- **Migrations must be reversible** — every `downgrade()` has a real body. `poe migrate-check` + `poe migrate-roundtrip` enforce this in CI.

See `../../CLAUDE.md` for the full anti-drift rule list (D1–D20) and the rationale behind each.

## Health endpoints

- `GET /api/health` — liveness, always 200, never touches the DB.
- `GET /api/health/ready` — readiness, composite check: DB ping + production env-var preflight. 503 on DB failure or config errors; 200 with warnings if config is degraded but operable.

## Tests

Tests use real migrations (`alembic downgrade base` → `upgrade head` once per session against `etfpulse_test`), not `Base.metadata.create_all`. Adding a model column without a migration breaks the suite immediately.

External APIs (SoSoValue, Binance) are exercised via the fixtures in `fixtures/` — `SOSOVALUE_USE_FIXTURES=true` is forced in tests. To refresh fixtures from live APIs, see `scripts/dev/README.md` (`capture_fixtures.py`).

## Deployment

`Dockerfile` runs `alembic upgrade head` then `exec uvicorn`. Migrations + code are one atomic deploy unit. CLAUDE.md → **Deployment (Coolify on Hetzner)** has the full env-var list and Telegram webhook setup.
