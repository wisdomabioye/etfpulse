"""/execute — open the ETFPulse Execute WebApp.

PR D.5.2 — DM-only command that replies with an inline keyboard
containing a `WebAppInfo(url=...)` button. The Telegram client opens
the URL inside its in-app browser, the React SPA detects
`window.Telegram.WebApp`, posts initData to `/api/auth/telegram/verify`,
and the user is logged in without re-running SIWE (D.5.3).

Why DM-only:
  - Group-launched WebApps don't carry stable user identity. The
    `initData.user` field may be absent or hold a synthetic value that
    doesn't match the user's real `chat.id`.
  - The bot's `/start` flow already restricts user-identity binding to
    DMs; matching that posture keeps the upsert path consistent.

Why no WebApp button when `frontend_url` is empty:
  - Telegram clients render `WebAppInfo(url=...)` with no URL as a
    broken button. Better UX to surface the misconfig as a text
    response than ship a click-and-fail button.

i18n keys (en + es follow existing pattern):
  - `cmd.execute.desc`        — slash-menu + /help entry
  - `execute.button_label`    — inline keyboard button text
  - `execute.dm_intro`        — text that accompanies the button
  - `execute.group_only`      — reply when invoked in a group
  - `execute.not_configured`  — reply when `frontend_url` is empty
"""

from __future__ import annotations

import structlog
from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from etfpulse.bot.i18n import resolve_lang, t
from etfpulse.config import settings

log = structlog.get_logger()


async def cmd_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the Execute-WebApp button (DM) or a redirect message (group)."""
    if update.effective_message is None or update.effective_chat is None:
        return
    lang = resolve_lang(update)

    # Group-chat: explain DM-only and don't try to render a WebApp
    # button (it would still launch but with broken user identity).
    if update.effective_chat.type != Chat.PRIVATE:
        log.info(
            "bot_execute_rejected_group",
            chat_id=update.effective_chat.id,
            chat_type=update.effective_chat.type,
        )
        await update.effective_message.reply_html(t("execute.group_only", lang=lang))
        return

    # Misconfigured deploy — empty OR non-HTTPS frontend_url means no
    # usable WebApp URL. Telegram clients reject non-HTTPS WebAppInfo
    # at click time; `WebAppInfo(url=...)` does NOT validate the scheme
    # at construction (verified against PTB), so we MUST gate here or
    # the bot ships a broken button. Both branches collapse to the
    # text fallback.
    if not settings.frontend_url or not settings.frontend_url.startswith("https://"):
        log.warning(
            "bot_execute_no_https_frontend_url",
            frontend_url=settings.frontend_url,
        )
        await update.effective_message.reply_html(t("execute.not_configured", lang=lang))
        return

    # The Execute SPA route under the configured frontend host.
    webapp_url = f"{settings.frontend_url.rstrip('/')}/execute"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=t("execute.button_label", lang=lang),
                    web_app=WebAppInfo(url=webapp_url),
                )
            ]
        ]
    )

    log.info(
        "bot_execute_button_shown",
        chat_id=update.effective_chat.id,
        webapp_url=webapp_url,
        lang=lang,
    )
    await update.effective_message.reply_html(
        t("execute.dm_intro", lang=lang),
        reply_markup=keyboard,
    )


__all__ = ["cmd_execute"]
