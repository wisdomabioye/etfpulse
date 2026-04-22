"""Alembic environment.

Async-native — uses the same `asyncpg` driver as the runtime app, so the URL
in `etfpulse.config.settings.database_url` works as-is. Coolify-style
`postgres://` URLs (no driver prefix) are normalized to `postgresql+asyncpg://`
so we don't need a second config field for migrations.

Migrations are run synchronously inside `connection.run_sync(...)` because
Alembic's `op.*` API is sync-only — async only buys us the shared driver and
URL.

Invocation surface: `uv run poe migrate` / `uv run poe revision "msg"`.
Never invoke from inside a running event loop (`asyncio.run` will refuse) —
migrations are a pre-app-start CLI step, not an in-app step.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from etfpulse.config import normalize_database_url, settings
from etfpulse.models import Base

# Alembic Config object — gives us access to alembic.ini values + CLI args.
config = context.config

# Set up Python logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Target metadata = everything declared on Base. Imports must happen before this
# line so all model modules are loaded into Base.metadata.tables.
target_metadata = Base.metadata


def _get_url() -> str:
    """Resolve the DB URL. Override via `-x db=...` for ad-hoc target DBs.

    `settings.database_url` is already normalised by config.py's validator;
    CLI overrides skip that path so we normalise them here via the shared
    helper.
    """
    cli_override = context.get_x_argument(as_dictionary=True).get("db")
    if cli_override:
        return normalize_database_url(cli_override)
    return settings.database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to the DB.

    Useful for code review / CI artifacts. Invoke with `alembic upgrade head --sql`.
    """
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    """Sync helper called inside `connection.run_sync(...)` from the async path."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect via async engine, then run sync migration steps inside it."""
    engine_config = config.get_section(config.config_ini_section, {})
    engine_config["sqlalchemy.url"] = _get_url()

    connectable = async_engine_from_config(
        engine_config,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
