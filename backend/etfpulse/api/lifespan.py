"""Application lifespan — composes startup and shutdown tasks.

Anti-drift rule (D2): every background process / external connection the app
owns (scheduler, Telegram bot, shared HTTP client, …) registers here as a
`StartupTask`. Never use `@app.on_event("startup"|"shutdown")` — those are
deprecated and scatter lifecycle logic across the codebase.

A `StartupTask` is an async context manager that takes the FastAPI app and
yields after its startup is done. Teardown runs on the way out. Each task
owns its own feature-flag check: if disabled, the task returns early. That
keeps the orchestrator (`lifespan` below) free of per-task knowledge.

Ordering: tasks enter in `STARTUP_TASKS` order and exit in reverse
(AsyncExitStack semantics). If task N fails to start, tasks 0..N-1 are torn
down cleanly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

import structlog
from fastapi import FastAPI

from etfpulse.api.logging_config import configure_logging

log = structlog.get_logger()

# A startup task: given the app, returns a context manager whose __aenter__
# runs startup work and __aexit__ runs teardown. Register tasks by appending
# to STARTUP_TASKS below (Stage 04 adds the scheduler; Stage 05 adds the bot).
type StartupTask = Callable[[FastAPI], AbstractAsyncContextManager[None]]

STARTUP_TASKS: list[StartupTask] = []


# Stage 04 — register the signal-cycle scheduler. Adding a StartupTask does
# NOT require editing `app.py` (anti-drift D14); this module is the one place
# the composition lives. Stage 05 will append the Telegram bot task below.
from etfpulse.bot.lifespan import start_bot  # noqa: E402
from etfpulse.pipeline.scheduler import start_scheduler  # noqa: E402

STARTUP_TASKS.extend([start_scheduler, start_bot])


def _log_config_preflight() -> None:
    """Log the env-var preflight report at startup.

    Errors → ERROR level (deploy logs surface them in red).
    Warnings → WARNING level.
    Clean (or non-production) → single INFO line, no noise.

    Logging only — does NOT abort startup. The readiness probe gates LB
    inclusion separately, so the pod can boot, log the misconfig, then
    sit out of rotation until ops fixes the env. That's the same model
    we use for transient DB failure (don't kill the container, take it
    out of LB rotation).
    """
    from etfpulse.api.config_check import check_config_health

    report = check_config_health()
    if report.errors:
        for msg in report.errors:
            log.error("config_preflight_error", message=msg)
    if report.warnings:
        for msg in report.warnings:
            log.warning("config_preflight_warning", message=msg)
    if not report.errors and not report.warnings:
        log.info("config_preflight_clean")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — drives startup and shutdown for the app.

    Logging is configured first so any failures during task startup are
    captured in the configured format. Config preflight runs next so
    deploy logs immediately show any misconfig before tasks attempt to
    use the missing values. Tasks then enter in order via AsyncExitStack;
    on the way out, they exit in reverse order.
    """
    configure_logging()
    log.info("app_startup", tasks=len(STARTUP_TASKS))
    _log_config_preflight()

    async with AsyncExitStack() as stack:
        for task in STARTUP_TASKS:
            await stack.enter_async_context(task(app))
        log.info("app_ready")
        yield
        log.info("app_shutdown_begin")

    log.info("app_shutdown_complete")
