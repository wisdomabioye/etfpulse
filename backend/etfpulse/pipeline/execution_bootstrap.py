"""Lifespan startup task for the execution surface (PR D.3).

Responsibilities:

  1. **Symbols cache bootstrap** — if `sodex_symbols` is empty at boot,
     do one synchronous refresh so the scheduler's first reconcile
     tick (60s later) can resolve `symbol_id` for any signal-driven
     order that fires immediately.
  2. **Orphan SUBMITTED log** — diagnostic-only count of orders that
     crashed between commit-to-SUBMITTED and the HTTP POST. Reconcile
     resolves these on its next tick; this just surfaces them in deploy
     logs so operators see the scale of any crash event.

Failure modes:
  - Bootstrap refresh fails (gateway down): logged + boot continues.
    The scheduler's daily symbols-refresh cron will retry, and reconcile
    will raise `SymbolNotResolved` (→ 503 from the route) until then.
  - Orphan scan failure is a DB read; if it errors, log + continue.

This module owns no scheduled jobs (those live in `scheduler.py`). It
runs ONCE per process boot.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from sqlalchemy import func, select

from etfpulse.adapters.sodex._http import SodexHttpClient
from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.config import settings
from etfpulse.db import async_session
from etfpulse.models import SodexSymbol
from etfpulse.pipeline.boot_recovery import flag_orphan_submitted_on_boot
from etfpulse.pipeline.symbols_refresh import refresh_sodex_symbols

log = structlog.get_logger(__name__)


def _build_clients() -> tuple[SodexSpotClient, SodexPerpsClient]:
    """Construct ephemeral clients for the bootstrap window. The
    scheduler builds its own long-lived clients on registration; these
    are scoped to the bootstrap call and closed via the `async with`
    in the StartupTask.
    """

    def _http(base: str) -> SodexHttpClient:
        return SodexHttpClient(
            base_url=base,
            timeout_seconds=settings.sodex_http_timeout_seconds,
            retry_max_attempts=settings.sodex_http_retry_max_attempts,
            retry_base_seconds=settings.sodex_http_retry_base_seconds,
        )

    spot_http = _http(settings.sodex_resolved_spot_base_url)
    perps_http = _http(settings.sodex_resolved_perps_base_url)
    return SodexSpotClient(spot_http), SodexPerpsClient(perps_http)


async def _bootstrap_symbols_if_empty() -> None:
    """If `sodex_symbols` table is empty, do one synchronous refresh."""
    async with async_session() as session:
        count = int((await session.execute(select(func.count(SodexSymbol.id)))).scalar_one())
        if count > 0:
            log.info("execution_bootstrap_symbols_skip", existing_count=count)
            return

        log.info("execution_bootstrap_symbols_seed_begin")
        spot_client, perps_client = _build_clients()
        # Use the venue clients' context-manager protocol so the
        # underlying httpx.AsyncClient closes cleanly on exit. The
        # double `async with` mirrors how D.2 documents the lifecycle.
        async with spot_client, perps_client:
            try:
                summary = await refresh_sodex_symbols(
                    session,
                    spot_client=spot_client,
                    perps_client=perps_client,
                )
                await session.commit()
                log.info("execution_bootstrap_symbols_seed_complete", **summary)
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                # Don't crash the lifespan — daily cron + reconcile-side
                # SymbolNotResolved surface the gap if it persists.
                log.error(
                    "execution_bootstrap_symbols_seed_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    exc_info=exc,
                )


async def _scan_orphan_submitted() -> None:
    """Diagnostic scan only — logs + counts."""
    async with async_session() as session:
        try:
            summary = await flag_orphan_submitted_on_boot(session)
            # Read-only — no commit needed, but be explicit.
            await session.rollback()
            if summary["orphan_submitted"] > 0:
                log.info("execution_bootstrap_orphan_scan", **summary)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "execution_bootstrap_orphan_scan_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )


@asynccontextmanager
async def start_execution_bootstrap(app: FastAPI) -> AsyncIterator[None]:
    """StartupTask — symbols bootstrap + orphan-scan diagnostics.

    Gated on `run_scheduler` because both responsibilities require the
    scheduler to be active (symbols need the daily-refresh fallback;
    orphan scan only matters if reconcile will run). When scheduler is
    off, this task is a no-op.
    """
    if not settings.run_scheduler:
        log.info("execution_bootstrap_disabled", reason="run_scheduler=false")
        yield
        return

    await _bootstrap_symbols_if_empty()
    await _scan_orphan_submitted()
    log.info("execution_bootstrap_ready")
    yield
    # Nothing to tear down — clients were ephemeral.
