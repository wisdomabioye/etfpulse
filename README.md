# ETFPulse

ETF-flow-driven signal intelligence for crypto traders. Ingests institutional ETF flows from SoSoValue, runs five detectors (flow anomaly, magnitude, acceleration, divergence, regime shift) to produce AI-explained signals, evaluates outcomes at 24h/72h against Binance prices for a public track record, and delivers everything via Telegram + a React dashboard.

## Stage status

- ✅ Stages 01–08 — foundation, backend core, ingestion, signal engine, Telegram bot, API + frontend, multi-signal intelligence, track record
- ✅ Production-hardening pass — sector-spotlight wired into regime classifier, reapers for stuck signals/deliveries, env-var preflight on readiness, price backfill, hardened 429 classifier
- ⏳ Stage 09 — SoDEX execution (Phase 3)
- ⏳ Stage 10 — Polish, demo, deployment (partial — Dockerfile + env conventions in place)

## Layout

```
etfpulse/
  backend/    Python 3.12 — FastAPI + async SQLAlchemy + APScheduler + python-telegram-bot
  frontend/   Vite + React 19 + TanStack Query — single-page dashboard
```

The bot, scheduler, and HTTP API all run in **one** FastAPI process by default. No Redis, no Celery, no separate worker — split with the `RUN_BOT` / `RUN_SCHEDULER` flags if needed.

## Quick start

```bash
# 1. Local Postgres + databases
createdb -U postgres etfpulse
createdb -U postgres etfpulse_test

# 2. Backend
cd backend
cp .env.example .env          # fill in SOSOVALUE_API_KEY, OPENROUTER_API_KEY, ADMIN_API_KEY
uv sync --extra dev
uv run poe migrate
uv run poe dev                # http://localhost:8000

# 3. Frontend (separate terminal)
cd frontend
pnpm install
pnpm dev                      # http://localhost:5173
```

Telegram bot is fully disabled until all four `TELEGRAM_*` env vars are set — useful for local dev without a bot account.

## Common tasks

```bash
# Backend
uv run poe test               # pytest against etfpulse_test DB
uv run poe check              # lint + format + typecheck + tests (matches CI)
uv run poe revision "msg"     # autogenerate an Alembic migration
uv run poe migrate-roundtrip  # verify the latest migration round-trips upgrade↔downgrade

# Frontend
pnpm run lint
pnpm run build
```

## Documentation

- `../CLAUDE.md` — the canonical architecture + conventions reference. Read this first.
- `../build_stages/ASSESSMENT.md` — why Vite (not Next.js), Postgres, APScheduler, cachetools (no Redis), Telegram bot inside FastAPI lifespan.
- `../build_stages/MULTI_USER_REVIEW.md` — multi-user data model already reflected in `backend/etfpulse/models/`.
- `../docs/API_REFERENCE.md` — real SoSoValue field names + quirks discovered during the spike.
- `backend/scripts/dev/README.md` — verification + fixture-capture utilities.

## Deployment

Container-based deploy behind a managed reverse proxy. `backend/Dockerfile` runs `alembic upgrade head` then `uvicorn`. Migrations and code deploys are one atomic unit — if a migration fails, the container exits non-zero and the platform rolls back. See CLAUDE.md → **Deployment** for the full env-var list and Telegram webhook setup.

## License

MIT
