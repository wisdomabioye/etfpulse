#!/usr/bin/env bash
# Round-trip check: upgrade → downgrade -1 → upgrade head.
#
# Issue #22. The static check (`check_migration_rollback.py`) catches stub
# downgrades; this one catches downgrades whose SQL is *broken* (refers to
# columns that no longer exist, drops indexes in the wrong order, etc).
#
# Runs against the TEST database — resolved via, in order:
#   1. `DATABASE_URL_TEST` env var (CI provides this; .env can too)
#   2. Python's `etfpulse.config.settings.database_url_test` (loaded from
#      `.env` by pydantic-settings) — the source of truth for dev machines
# We deliberately do NOT fall through to `database_url` — a local dev
# running this without DATABASE_URL_TEST set would otherwise downgrade-
# then-re-upgrade their *dev* DB and risk corrupting real state.
#
# Drops back ONE revision (not `base`). Reason: downgrading to base
# exercises every migration in history; for the rollback invariant we
# only need to verify that the *most recently merged* migration round-
# trips cleanly, which is what a deploy rollback would actually exercise.
#
# Run via `uv run poe migrate-roundtrip`.

set -euo pipefail

# Resolve the test DB URL — env wins, then ask pydantic settings.
if [[ -z "${DATABASE_URL_TEST:-}" ]]; then
    DATABASE_URL_TEST="$(uv run python -c 'from etfpulse.config import settings; print(settings.database_url_test)')"
fi

if [[ -z "${DATABASE_URL_TEST}" ]]; then
    echo "ERROR: DATABASE_URL_TEST is empty. Set it in .env or your shell." >&2
    exit 2
fi

# Pass via `-x db=...` so alembic's env.py picks the test URL even when
# `DATABASE_URL` (read by `settings.database_url` at import time) points
# at the dev DB. This is the same override Alembic's documentation
# recommends for ad-hoc target DBs; see migrations/env.py:_get_url.
ALEMBIC="uv run alembic -x db=${DATABASE_URL_TEST}"

echo "→ Bring DB to head"
${ALEMBIC} upgrade head >/dev/null

echo "→ Downgrade one revision"
${ALEMBIC} downgrade -1 >/dev/null

echo "→ Upgrade back to head"
${ALEMBIC} upgrade head >/dev/null

echo "OK — latest migration round-trips cleanly (upgrade → downgrade -1 → upgrade head)."
