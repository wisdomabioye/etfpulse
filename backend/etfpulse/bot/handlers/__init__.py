"""Bot command handlers registration point.

Anti-drift rule (D16): handler modules live in `bot/handlers/*.py` and are
wired into the PTB `Application` exclusively via `register_handlers()` below.
Never call `application.add_handler()` from outside this module — keeps the
registration surface greppable and auditable.

Each handler module exports one or two `cmd_*` functions. This module's job
is the composition: listing every command name + handler pair.
"""

from __future__ import annotations

from telegram.ext import Application, CommandHandler

from etfpulse.bot.handlers.help import cmd_help, cmd_track_record
from etfpulse.bot.handlers.prefs import cmd_prefs
from etfpulse.bot.handlers.start import cmd_start
from etfpulse.bot.handlers.subscribe import cmd_subscribe, cmd_unsubscribe

# Single source of truth — command name → handler. Ordering doesn't matter
# (PTB matches by exact command name), but alphabetical keeps diffs readable.
_COMMANDS: list[tuple[str, object]] = [
    ("help", cmd_help),
    ("prefs", cmd_prefs),
    ("start", cmd_start),
    ("subscribe", cmd_subscribe),
    ("track_record", cmd_track_record),  # slash-command names can't have dashes
    ("unsubscribe", cmd_unsubscribe),
]


def register_handlers(application: Application) -> None:
    """Wire every bot handler onto the PTB Application.

    Called once from `bot/lifespan.py:start_bot` after `Application.builder()`
    and before `application.initialize()`.
    """
    for name, handler in _COMMANDS:
        application.add_handler(CommandHandler(name, handler))  # type: ignore[arg-type]


__all__ = ["register_handlers"]
