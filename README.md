# ETFPulse

**Bound the loss. Let the rest compound.**

ETF-flow-driven signal intelligence for crypto traders. Ingests institutional ETF flows from SoSoValue, runs five detectors (flow anomaly, magnitude, acceleration, divergence, regime shift) to produce AI-explained signals, scores outcomes against the signal's own validity window for a public track record, delivers everything via Telegram, and lets users execute on SoDEX with wallet-side signing — the backend never holds a private key.

## What you get

- **Signals** with AI reasoning, suggested entry/stop/target, and risk callouts — public dashboard + Telegram alerts.
- **Track record** that scores against the AI's claimed direction + horizon, not a fixed 72h window. Per-detector precision leaderboard, per-confidence-bucket calibration curve.
- **Regime context** — the system classifies the market as Markup / Markdown / Accumulation / Distribution / Uncertain on every cycle, and folds the classification into detection thresholds.
- **One-tap trading** — Telegram bot's `/execute` opens the SPA in a WebApp; SIWE wallet binding → prepare order → wallet signs EIP-712 typed-data → backend submits to SoDEX. Paper-trade mode for first runs.

## Stage status

- ✅ **Stages 01–08** — foundation, ingestion, signal engine, Telegram bot, API + frontend, multi-signal intelligence, track record
- ✅ **Production-hardening pass** — sector-spotlight regime feed, reapers, env-var preflight, price backfill, hardened 429 classifier
- ✅ **Stage 09** — SoDEX execution (D.1 EIP-712 builders, D.2 HTTP adapters, D.3 execution surface + risk + reconcile, D.4 JWT/SIWE wallet binding, D.5 Telegram WebApp entry)
- ✅ **Predictive robustness (I.1–I.5)** — calibration curve, multi-factor confirmation, per-detector precision, MARKET composite scoring, backtest harness
- 🚧 **Stage 10** — polish & demo & deployment (in progress; testnet smoke pending operator)

## Layout

```
etfpulse/
  backend/      Python 3.12 — FastAPI + async SQLAlchemy + APScheduler + python-telegram-bot
  frontend/     Vite + React 19 + TanStack Query — single-page dashboard + execution surface
```

Bot, scheduler, and HTTP API run in **one** FastAPI process by default. No Redis, no Celery — split with `RUN_BOT` / `RUN_SCHEDULER` flags if you need to.

## Quick start

```bash
# 1. Local Postgres + databases (one-time)
createdb -U postgres etfpulse
createdb -U postgres etfpulse_test

# 2. Backend
cd backend
cp .env.example .env          # fill in SOSOVALUE_API_KEY, OPENROUTER_API_KEY, ADMIN_API_KEY, JWT_SECRET
uv sync --extra dev
uv run poe migrate
uv run poe dev                # http://localhost:8000

# 3. Frontend (separate terminal)
cd frontend
pnpm install
pnpm dev                      # http://localhost:5173
```

Telegram bot is fully disabled until all four `TELEGRAM_*` env vars are set — useful for local dev without a bot account. Wallet trading is disabled until `VITE_WALLETCONNECT_PROJECT_ID` is set on the FE; the page renders a graceful "wallet not configured" state.

## Common tasks

```bash
# Backend
uv run poe test               # pytest against etfpulse_test DB (1691 tests at last check)
uv run poe check              # lint + format + typecheck + migration round-trip + env-drift + tests (matches CI)
uv run poe revision "msg"     # autogenerate an Alembic migration
uv run poe migrate-roundtrip  # verify the latest migration round-trips upgrade↔downgrade
uv run poe env-drift-check    # verify every config.py field is documented in .env.example
uv run python scripts/backtest.py --start 2026-01-01 --end 2026-03-31 \
  --config-override '{"magnitude": {"percentile_threshold": 0.9}}'

# Frontend
pnpm run lint
pnpm run build
pnpm test:run                 # vitest, 40 tests at last check
```

## Try the live execution flow

1. Open the SPA → tap **Trade** in the nav (or `/execute` directly).
2. If you don't have a Telegram-issued JWT yet, the page redirects to `/login` → **Connect Wallet** → wallet app opens via WalletConnect → approve → return → **Sign in with Ethereum**.
3. Once bound, the Execute page shows ApiKeyForm (one-time per venue) → OrderForm. Place a paper-trade order first (operator must flip the flag); confirm the round-trip; then go live.

The Telegram path is the same, but with the entry point being a chat command (`/execute`) and the wallet handoff happening inside Telegram's in-app browser via WalletConnect deep links.

## Documentation

- [`../CLAUDE.md`](../CLAUDE.md) — canonical architecture + conventions reference. **Read this first** before making structural changes.
- [`../build_stages/ASSESSMENT.md`](../build_stages/ASSESSMENT.md) — why Vite (not Next.js), Postgres, APScheduler, cachetools (no Redis), Telegram bot inside FastAPI lifespan.
- [`../docs/sodex/D5_RUNBOOK.md`](../docs/sodex/D5_RUNBOOK.md) — Stage 09 operator runbook: env setup, dev HTTPS tunnels (ngrok / cloudflare), wallet recovery, API key rotation, webhook secret rotation.
- [`../docs/sodex/D5_SMOKE.md`](../docs/sodex/D5_SMOKE.md) — 11-step manual smoke from `/start` to position verification.
- [`../docs/API_REFERENCE.md`](../docs/API_REFERENCE.md) — real SoSoValue field names + quirks discovered during the integration spike.
- [`backend/scripts/dev/README.md`](backend/scripts/dev/README.md) — verification + fixture-capture utilities.

## Conventions to respect

- **No key custody.** The backend never holds, generates, or signs with private keys. SoDEX execution flows through wallet-side signing only. Anti-drift rules 27 + 28 enforce this in tests.
- **One-tier product.** No premium, no token, no white-label, no auto-execution. The product surface is the same for every user.
- **Multi-user from day 1.** `User` / `NotificationChannel` / `TelegramGroup` already in the schema; do not regress toward single-user.
- **Idempotency everywhere.** Signals dedupe by `(fingerprint, signal_date)`. Migrations are reversible (CI guard). Backfills are NULL-only.

## Deployment

Container-based deploy behind a managed reverse proxy. `backend/Dockerfile` runs `alembic upgrade head` then `uvicorn`. Migrations and code deploys are one atomic unit — if a migration fails, the container exits non-zero and the platform rolls back. See [`../CLAUDE.md`](../CLAUDE.md#deployment-coolify-on-hetzner) for the full env-var matrix.

After the first deploy, run the historical backfills once:

```bash
uv run python scripts/backfill_signal_prices.py
uv run python scripts/backfill_confirmation.py
```

Both are idempotent + NULL-only — re-run until each reports `Candidates: 0`.

## License

MIT
