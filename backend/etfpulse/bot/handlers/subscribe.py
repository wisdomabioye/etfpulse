"""/subscribe and /unsubscribe — flip `is_active` on the User or TelegramGroup.

Soft-delete semantics (edge case 25): /unsubscribe does NOT delete any data,
just sets `is_active=false`. Preferences are preserved so /subscribe later
resumes exactly where they left off. /start also re-activates (see start.py).
"""

from __future__ import annotations

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from etfpulse.bot.handlers._common import get_or_create_target
from etfpulse.db import async_session

log = structlog.get_logger()


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        target = await get_or_create_target(session, update)
        target.obj.is_active = True
        target.obj.pref_paused = False
        await session.commit()

    log.info("bot_subscribe_handled", kind=target.kind, id=target.obj.id)
    await _reply(
        update,
        "✅ <b>Subscribed.</b> You'll receive new signals matching your preferences.",
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        target = await get_or_create_target(session, update)
        target.obj.is_active = False
        await session.commit()

    log.info("bot_unsubscribe_handled", kind=target.kind, id=target.obj.id)
    await _reply(
        update,
        "🔕 <b>Unsubscribed.</b> Your preferences are kept — /subscribe again any time to resume.",
    )


async def _reply(update: Update, text: str) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_html(text)


__all__ = ["cmd_subscribe", "cmd_unsubscribe"]
