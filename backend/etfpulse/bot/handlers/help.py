"""/help and /track-record — static responses, no DB state.

`/help` — command list.
`/track-record` — stub pointing at Stage 08 (SignalOutcome-driven stats).
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
    "• <code>/track-record</code> — public signal performance (coming soon)\n"
    "• <code>/help</code> — this message"
)

_TRACK_RECORD_STUB = (
    "📊 <b>Track record coming in Stage 08.</b>\n\n"
    "Once we have evaluated 24h + 72h outcomes for a meaningful number of "
    "signals, this command will show hit-rate, average return, and "
    "per-detector performance."
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, _HELP_TEXT)


async def cmd_track_record(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, _TRACK_RECORD_STUB)


async def _reply(update: Update, text: str) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_html(text)


__all__ = ["cmd_help", "cmd_track_record"]
