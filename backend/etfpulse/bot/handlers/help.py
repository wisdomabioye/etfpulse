"""/help — static command list, no DB state.

`/track-record` and `/performance` were stubs here in Stage 7; they moved
to `bot/handlers/track_record.py` in Stage 8-P9 when they grew DB lookups.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

_HELP_TEXT = (
    "<b>ETFPulse commands</b>\n\n"
    "• <code>/start</code> — register / re-activate notifications\n"
    "• <code>/prefs</code> — show current preferences\n"
    "• <code>/prefs assets BTC,ETH</code> — set which assets to watch\n"
    "• <code>/prefs confidence 7</code> — minimum confidence (1-10)\n"
    "• <code>/subscribe</code> / <code>/unsubscribe</code> — resume / pause\n"
    "• <code>/track-record</code> (or <code>/performance</code>) — public signal performance\n"
    "• <code>/help</code> — this message"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_html(_HELP_TEXT)


__all__ = ["cmd_help"]
