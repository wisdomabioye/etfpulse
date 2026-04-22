# Production image for Coolify deploys. Coolify handles reverse proxy (Traefik),
# health probes (HTTP against /api/health), and Postgres provisioning.
# Migrations run automatically on container start — one atomic deploy unit.

FROM python:3.12-slim AS base

# PYTHONUNBUFFERED: flush stdout/stderr immediately so Coolify log streams work.
# PYTHONDONTWRITEBYTECODE: no .pyc files clutter the image.
# UV_COMPILE_BYTECODE: precompile installed packages for faster startup.
# UV_LINK_MODE=copy: avoid hardlink errors across filesystem boundaries in containers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install deps without the project first — this layer stays cached until
# pyproject.toml or uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the source + install the project itself.
COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8000

# On every container start: run migrations, then exec uvicorn.
# - `alembic upgrade head` is idempotent — already-applied migrations are a no-op
# - If a migration fails, the shell exits non-zero and Coolify reports the failure
#   (container doesn't start → deploy is marked broken → rollback is available)
# - `exec` replaces the shell with uvicorn so signals (SIGTERM) propagate correctly
CMD ["sh", "-c", ".venv/bin/alembic upgrade head && exec .venv/bin/uvicorn etfpulse.app:app --host 0.0.0.0 --port 8000"]
