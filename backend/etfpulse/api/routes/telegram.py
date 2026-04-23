"""Telegram webhook receiver.

Single route: POST /api/telegram/webhook/{suffix}. Three ordered gates:
    1. `verify_webhook_suffix` — 404 if path suffix ≠ config (scanner hides)
    2. `verify_bot_enabled`    — 404 if bot is disabled (same scanner-view)
    3. `verify_telegram_secret` — 401 if secret header mismatches

The handler then parses the JSON body into a PTB `Update` and dispatches via
`application.process_update(update)`. Returns 200 immediately — Telegram
doesn't care about the body, only that we acknowledged receipt. Heavy work
(AI calls etc.) never happens inline; handlers use `async_session()` to
persist state, and anything longer-running is queued for the scheduler.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from telegram import Update

from etfpulse.api.deps import (
    verify_bot_enabled,
    verify_telegram_secret,
    verify_webhook_suffix,
)

log = structlog.get_logger()
router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post(
    "/webhook/{suffix}",
    # Order matters: suffix → bot-enabled → secret. See deps.py for rationale.
    dependencies=[
        Depends(verify_webhook_suffix),
        Depends(verify_bot_enabled),
        Depends(verify_telegram_secret),
    ],
    # Internal endpoint; not product API surface.
    include_in_schema=False,
)
async def telegram_webhook(suffix: str, request: Request) -> dict[str, bool]:
    """Receive one update from Telegram, dispatch through PTB handlers."""
    application = request.app.state.bot_application
    body = await request.json()
    update = Update.de_json(body, application.bot)

    # PTB's handler chain. Exceptions inside handlers don't propagate here —
    # PTB logs them via its own error-handler mechanism. See bot/handlers/
    # for the registered handlers.
    await application.process_update(update)

    log.info("telegram_webhook_processed", update_id=body.get("update_id"))
    return {"ok": True}
