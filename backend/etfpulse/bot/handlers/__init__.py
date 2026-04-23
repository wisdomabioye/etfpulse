"""Bot command handlers registration point.

Anti-drift rule (D16): handler modules live in `bot/handlers/*.py` and are
wired into the PTB `Application` exclusively via `register_handlers()` below.
Never call `application.add_handler()` from outside this module — keeps the
registration surface greppable and auditable.

Stage 05 fills this in. Current state: placeholder that registers nothing,
so the bot boots cleanly and the webhook receiver route can call
`application.process_update(update)` without error (it just won't dispatch
to anything yet).
"""

from __future__ import annotations

from telegram.ext import Application


def register_handlers(application: Application) -> None:
    """Wire every bot handler onto the PTB Application.

    Called once from `bot/lifespan.py:start_bot` after `Application.builder()`
    and before `application.initialize()`. Stage 05b (#55) adds the concrete
    /start, /subscribe, /unsubscribe, /prefs, /help handlers.
    """
    # Stage 05b appends `application.add_handler(CommandHandler("start", ...))`
    # etc. here. For now, intentionally empty — the wiring path itself is
    # what gets tested in Stage 05a.
    return None
