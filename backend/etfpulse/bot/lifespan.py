"""Bot StartupTask — registers with FastAPI lifespan per anti-drift D2.

On startup:
    1. Early-return no-op if `settings.is_bot_enabled` is False. Fully disables
       the bot: no PTB Application constructed, no webhook registered, and
       the webhook receiver route (#65) returns 404 because app.state.bot_application
       is absent. Matches Resolution R12 + W12.
    2. Build PTB `Application` with just the bot token.
    3. Register handlers via `bot.handlers.register_handlers(application)` —
       the single composition point (D16). Stage 05b fills this in.
    4. `await application.initialize()` — required for `process_update()` to
       work from the webhook route. We do NOT call `application.start()`
       because we don't use PTB's internal update queue — the webhook route
       dispatches directly via `process_update()`.
    5. Assemble webhook URL `{public_url}/api/telegram/webhook/{suffix}` and
       push config to Telegram via `telegram_client.set_webhook(...)`. If
       Telegram is unreachable, log a warning and proceed — better to boot
       the app and retry the registration later than block startup.
    6. Stash the Application on `app.state.bot_application` so the webhook
       route can reach it.

On shutdown:
    `await application.shutdown()` — flushes any in-flight internal state.
    We do NOT call `delete_webhook` on shutdown — a deploy rolling restart
    would otherwise race: old container deletes webhook, new container
    re-creates it, but any updates Telegram sent during the gap are lost.
    Leaving the webhook registered across restarts means Telegram just
    retries briefly — far safer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from telegram.error import TelegramError as PTBTelegramError
from telegram.ext import Application

from etfpulse.adapters.telegram import telegram_client
from etfpulse.bot.handlers import register_handlers
from etfpulse.config import settings

log = structlog.get_logger()

# Matches Decision D1c — we only care about `message` (commands + text) and
# `my_chat_member` (for auto-detecting when bot is added/removed from groups).
# Filtering here reduces both bandwidth and attack surface (Telegram won't
# send us update types we don't handle).
_ALLOWED_UPDATES = ["message", "my_chat_member"]


def _webhook_url() -> str:
    """Assemble the full webhook URL Telegram will POST to.

    Example: `https://app.example.com/api/telegram/webhook/abc123xyz`
    where `abc123xyz` is `settings.telegram_webhook_url_suffix` (the random
    unguessable path component that is our primary webhook defense).
    """
    base = settings.telegram_public_url.rstrip("/")
    return f"{base}/api/telegram/webhook/{settings.telegram_webhook_url_suffix}"


@asynccontextmanager
async def start_bot(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI StartupTask — one of `STARTUP_TASKS` in api/lifespan.py."""
    if not settings.is_bot_enabled:
        log.info(
            "bot_disabled",
            # Log which specific gate failed to help diagnose config drift.
            run_bot=settings.run_bot,
            has_token=bool(settings.telegram_bot_token),
            has_public_url=bool(settings.telegram_public_url),
            has_secret=bool(settings.telegram_webhook_secret),
            has_url_suffix=bool(settings.telegram_webhook_url_suffix),
        )
        yield
        return

    application = Application.builder().token(settings.telegram_bot_token).build()
    register_handlers(application)

    # `initialize()` calls Telegram's `getMe` internally — can fail on invalid
    # token, network blip, or Telegram outage. If it fails, we can't safely
    # `process_update` (PTB's handler chain assumes initialize completed), so
    # we do NOT attach the Application to app.state. Result: webhook route
    # returns 404 ("bot disabled"), app boots otherwise-healthy, next container
    # restart retries. Aligns with set_webhook's log-and-proceed contract.
    try:
        await application.initialize()
    except PTBTelegramError as exc:
        log.warning(
            "bot_initialize_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        # Skip set_webhook too (would also fail) and yield without attaching.
        yield
        return
    log.info("bot_application_initialized")

    webhook_url = _webhook_url()
    try:
        await telegram_client.set_webhook(
            url=webhook_url,
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=_ALLOWED_UPDATES,
        )
    except PTBTelegramError as exc:
        # Don't block app startup on a transient Telegram outage. The webhook
        # route still works locally; ops can re-trigger registration on next
        # deploy once Telegram's back. Log so it's visible in Coolify.
        log.warning(
            "bot_set_webhook_failed",
            url=webhook_url,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    app.state.bot_application = application
    log.info("bot_started", webhook_url=webhook_url, allowed_updates=_ALLOWED_UPDATES)

    try:
        yield
    finally:
        log.info("bot_shutdown_begin")
        await application.shutdown()
        log.info("bot_shutdown_complete")
