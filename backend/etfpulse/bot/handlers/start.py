"""/start — register User+NotificationChannel (DM) or TelegramGroup (group).

Idempotent — sending /start twice just re-welcomes the user. On first run,
creates rows with env-driven default preferences (see config.py delivery_*).
"""

from __future__ import annotations

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from etfpulse.bot.handlers._common import get_or_create_target
from etfpulse.db import async_session

log = structlog.get_logger()


_DM_WELCOME = (
    "👋 <b>Welcome to ETFPulse</b>\n\n"
    "You'll receive crypto ETF flow signals here when our detectors fire. "
    "Default preferences: <code>BTC, ETH</code>, confidence ≥ 6.\n\n"
    "Commands:\n"
    "• <code>/prefs assets BTC,ETH</code> — set which assets to watch\n"
    "• <code>/prefs confidence 7</code> — set minimum confidence (1-10)\n"
    "• <code>/unsubscribe</code> / <code>/subscribe</code> — pause / resume\n"
    "• <code>/help</code> — this list again"
)

_GROUP_WELCOME = (
    "👋 <b>ETFPulse is now monitoring this group</b>\n\n"
    "Signals will be posted here when our detectors fire. Any member can "
    "configure with:\n"
    "• <code>/prefs assets BTC,ETH</code>\n"
    "• <code>/prefs confidence 7</code>\n"
    "• <code>/help</code> for the full command list"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        target = await get_or_create_target(session, update)
        # /start is ALSO a "re-subscribe" action — if the user previously
        # /unsubscribed, typing /start flips them back on. Less surprising
        # than "you need to /subscribe too".
        target.obj.is_active = True
        target.obj.pref_paused = False
        await session.commit()

    log.info(
        "bot_start_handled",
        kind=target.kind,
        id=target.obj.id,
        chat_id=update.effective_chat.id if update.effective_chat else None,
    )
    welcome = _DM_WELCOME if target.kind == "user" else _GROUP_WELCOME
    await _reply(update, welcome)


async def _reply(update: Update, text: str) -> None:
    """Thin wrapper — makes tests easier to mock and future delivery of
    HTML/Markdown consistent."""
    if update.effective_message is None:
        return
    await update.effective_message.reply_html(text)


# Re-export so the registration module can import from one place.
__all__ = ["cmd_start"]
