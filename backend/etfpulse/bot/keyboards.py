"""Inline keyboard builders for the bot (issue #38).

**/prefs no-args** — quick-toggle keyboard for asset selection + 1-tap
confidence adjustments + subscribe/unsubscribe. Buttons use
`callback_data` strings parsed by `bot/handlers/callbacks.py`.

The signal-alert "View on web" URL button lives in `pipeline/delivery.py`
(not here) so the `pipeline/` package doesn't have to import from `bot/`
— delivery already owns the rest of the outbound message rendering.

Callback-data wire format:
    `<prefix>:<action>[:<arg>]`

    `prefs:toggle_asset:BTC`
    `prefs:conf_inc`
    `prefs:conf_dec`
    `prefs:pause`
    `prefs:resume`

The prefix is what `callbacks.py` dispatches on. Keep callback strings
short — Telegram's limit is 64 bytes per `callback_data`, and a longer
ID/asset list would blow that quickly.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from etfpulse.bot.constants import VALID_ASSETS
from etfpulse.models import TelegramGroup, User

# Callback-data prefix routed to `cb_prefs_*` handlers.
PREFS_PREFIX = "prefs"


def build_prefs_keyboard(target: User | TelegramGroup) -> InlineKeyboardMarkup:
    """Quick-toggle keyboard for /prefs.

    Layout (3 rows):
        Row 1 — one toggle per known asset; the currently-enabled assets
            get a ✅ prefix.
        Row 2 — Confidence ➖ <current> ➕ — three buttons; the middle one
            is a static label (callback `prefs:noop`) so the operator
            sees the value between the +/- controls.
        Row 3 — Subscribe / Unsubscribe pair; render only the action
            that's actually available given the current state (avoids
            users tapping a no-op).
    """
    enabled_assets = set(target.pref_assets or [])
    row_assets = [
        InlineKeyboardButton(
            ("✅ " if asset in enabled_assets else "▫️ ") + asset,
            callback_data=f"{PREFS_PREFIX}:toggle_asset:{asset}",
        )
        for asset in VALID_ASSETS
    ]
    row_confidence = [
        InlineKeyboardButton("➖", callback_data=f"{PREFS_PREFIX}:conf_dec"),
        InlineKeyboardButton(
            f"Conf: {target.pref_min_confidence}/10",
            # Inert label — handler routes this to a silent answer_callback_query.
            callback_data=f"{PREFS_PREFIX}:noop",
        ),
        InlineKeyboardButton("➕", callback_data=f"{PREFS_PREFIX}:conf_inc"),
    ]

    row_subscription: list[InlineKeyboardButton] = []
    if target.is_active:
        row_subscription.append(
            InlineKeyboardButton("⏸ Pause", callback_data=f"{PREFS_PREFIX}:pause")
        )
    else:
        row_subscription.append(
            InlineKeyboardButton("▶️ Resume", callback_data=f"{PREFS_PREFIX}:resume")
        )

    return InlineKeyboardMarkup([row_assets, row_confidence, row_subscription])


__all__ = ["PREFS_PREFIX", "build_prefs_keyboard"]
