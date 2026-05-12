"""/start — register User+NotificationChannel (DM) or TelegramGroup (group).

Idempotent — sending /start twice just re-welcomes the user. On first run,
creates rows with env-driven default preferences (see config.py delivery_*).
"""

from __future__ import annotations

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from etfpulse.bot.handlers._common import get_or_create_target
from etfpulse.bot.i18n import render_command_list, resolve_lang, t
from etfpulse.db import async_session

log = structlog.get_logger()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        target = await get_or_create_target(session, update)
        # /start is ALSO a "re-subscribe" action — if the user previously
        # /unsubscribed, typing /start flips them back on. Less surprising
        # than "you need to /subscribe too".
        target.obj.is_active = True
        target.obj.pref_paused = False
        await session.commit()

    lang = resolve_lang(update)
    log.info(
        "bot_start_handled",
        kind=target.kind,
        id=target.obj.id,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        lang=lang,
    )
    # Interpolate the target's ACTUAL prefs into the welcome — for a
    # returning user this is more accurate than the global defaults
    # (their /prefs may have customised things since first /start).
    # `{command_list}` is required by BOTH welcome strings; `{assets}` and
    # `{confidence}` only by welcome.dm. Extra kwargs to `.format()` are
    # ignored, so passing all three uniformly keeps the call site DRY
    # across kind="user" and kind="group".
    key = "welcome.dm" if target.kind == "user" else "welcome.group"
    assets_str = ", ".join(target.obj.pref_assets) if target.obj.pref_assets else "all"
    await _reply(
        update,
        t(
            key,
            lang=lang,
            assets=assets_str,
            confidence=target.obj.pref_min_confidence,
            command_list=render_command_list(lang),
        ),
    )


async def _reply(update: Update, text: str) -> None:
    """Thin wrapper — makes tests easier to mock and future delivery of
    HTML/Markdown consistent."""
    if update.effective_message is None:
        return
    await update.effective_message.reply_html(text)


# Re-export so the registration module can import from one place.
__all__ = ["cmd_start"]
