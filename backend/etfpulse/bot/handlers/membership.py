"""my_chat_member handler — auto-register groups when the bot is added,
auto-deactivate when removed (issue #35).

Before this handler, a group only became a TelegramGroup row after a
human typed /start in the group. That's an awkward dependency — the
group admin who added the bot might not be the one who types /start,
and signals would silently fail to fan out to the group in the meantime.

Telegram sends `my_chat_member` updates whenever the bot's membership /
permissions in a chat change. The update is bot-scoped by design (the
sibling `chat_member` update is for other members and needs admin
rights to receive); we don't need to filter by user id.

Transitions we care about (group / supergroup only):
    {left, banned}                → {member, administrator, restricted}
        → bot added. Register/reactivate the group + post a welcome.
    {member, administrator, creator, restricted} → {left, banned}
        → bot removed. Soft-delete (is_active=false). Don't drop the row
          — preserves the group's preferences if it gets re-added.
    administrator ↔ member (promotion / demotion)
        → no-op for now. We don't enforce admin-only group commands yet
          (issue #39 — separate task).

DMs and channels are ignored — DM registration happens via /start, and
we don't support channels.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telegram import Chat, ChatMember, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from etfpulse.config import settings
from etfpulse.db import async_session
from etfpulse.models import TelegramGroup

log = structlog.get_logger()

# Statuses where the bot is effectively IN the chat. `restricted` is
# included because a restricted bot is still a member that can be
# promoted; it's NOT the same as `left`.
_PRESENT_STATUSES = frozenset(
    {
        ChatMember.MEMBER,
        ChatMember.ADMINISTRATOR,
        ChatMember.OWNER,
        ChatMember.RESTRICTED,
    }
)

_GROUP_WELCOME = (
    "👋 <b>ETFPulse is now monitoring this group</b>\n\n"
    "Signals will be posted here when our detectors fire. Configure with:\n"
    "• <code>/prefs assets BTC,ETH</code>\n"
    "• <code>/prefs confidence 7</code>\n"
    "• <code>/help</code> for the full command list"
)


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch on the bot-membership transition.

    Side effects are committed BEFORE the welcome reply so that a Telegram
    send failure doesn't roll back the DB state — the group is registered
    even if the welcome message can't be sent (network blip, permissions
    too restrictive). The send is wrapped in a broad TelegramError catch
    for the same reason.
    """
    event = update.my_chat_member
    chat = update.effective_chat
    if event is None or chat is None:
        return
    if chat.type not in (Chat.GROUP, Chat.SUPERGROUP):
        # DMs handled by /start; channels not supported.
        return

    was_in = event.old_chat_member.status in _PRESENT_STATUSES
    now_in = event.new_chat_member.status in _PRESENT_STATUSES

    if not was_in and now_in:
        await _on_added(chat, update)
    elif was_in and not now_in:
        await _on_removed(chat)
    # else: promotion/demotion or no-change — no-op.


async def _on_added(chat: Chat, update: Update) -> None:
    """Bot was added to (or re-added to) a group. Upsert the row, flip
    is_active=true (in case this is a re-add of a previously-removed
    group), then welcome."""
    async with async_session() as session:
        result = await session.execute(
            select(TelegramGroup).where(TelegramGroup.chat_id == chat.id)
        )
        group = result.scalar_one_or_none()

        if group is None:
            group = TelegramGroup(
                chat_id=chat.id,
                title=chat.title,
                pref_assets=settings.delivery_default_assets_list,
                pref_min_confidence=settings.delivery_default_min_confidence,
            )
            session.add(group)
            try:
                await session.commit()
                created = True
            except IntegrityError:
                # Concurrent my_chat_member for the same chat won the race on
                # the chat_id UNIQUE constraint. Roll back and re-resolve so
                # we still reactivate (the winning insert may have set
                # is_active correctly, but in case the row pre-existed and
                # the OTHER tx took the "else" branch — we can't know — be
                # defensive and run the reactivate path). Same idempotency
                # pattern as `_common.py:_resolve_or_create_group`.
                await session.rollback()
                result = await session.execute(
                    select(TelegramGroup).where(TelegramGroup.chat_id == chat.id)
                )
                group = result.scalar_one()
                group.title = chat.title
                group.is_active = True
                group.pref_paused = False
                await session.commit()
                created = False
        else:
            # Re-add: refresh title (could have changed) and reactivate.
            group.title = chat.title
            group.is_active = True
            group.pref_paused = False
            await session.commit()
            created = False

    log.info(
        "bot_added_to_group",
        chat_id=chat.id,
        title=chat.title,
        created=created,
    )

    # Welcome AFTER commit — see module docstring.
    if update.effective_message is not None:
        try:
            await update.effective_message.reply_html(_GROUP_WELCOME)
        except TelegramError as exc:
            # Welcome failed (no send permission, rate limit, etc) — log
            # and move on. The group is already registered; signals will
            # still try to fan out to it.
            log.warning("group_welcome_send_failed", chat_id=chat.id, error=str(exc))


async def _on_removed(chat: Chat) -> None:
    """Bot was removed/kicked. Soft-delete — preserves prefs if re-added."""
    async with async_session() as session:
        result = await session.execute(
            select(TelegramGroup).where(TelegramGroup.chat_id == chat.id)
        )
        group = result.scalar_one_or_none()
        if group is None:
            # Bot was removed from a group we never registered (added before
            # the my_chat_member handler shipped). Nothing to do.
            log.info("bot_removed_from_unknown_group", chat_id=chat.id)
            return
        group.is_active = False
        await session.commit()

    log.info("bot_removed_from_group", chat_id=chat.id)


__all__ = ["handle_my_chat_member"]
