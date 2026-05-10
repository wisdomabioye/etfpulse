"""Shared helpers for bot command handlers.

The DM-vs-group branching + target lookup is the cross-cutting concern — we
extract it so each handler stays readable. Anti-drift note: these helpers
are INTERNAL to the handlers package (leading underscore in the filename
conveys "not public API"); don't import from them outside `bot/handlers/*`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Chat, ChatMember, Update
from telegram import User as TgUser
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from etfpulse.bot.constants import VALID_ASSETS
from etfpulse.config import settings
from etfpulse.models import ChannelType, NotificationChannel, TelegramGroup, User

log = structlog.get_logger()

# Set form of VALID_ASSETS for O(1) membership in `parse_asset_list`.
# Single source of truth lives in `bot/constants.py`; this is a derived
# view, not a duplicate.
_VALID_ASSETS_SET = frozenset(VALID_ASSETS)

# ChatMember statuses that grant admin powers in a group/supergroup.
# `RESTRICTED` and `MEMBER` are NOT included — regular members can read
# but can't change group settings. Used by `is_group_admin` below.
_ADMIN_STATUSES = frozenset({ChatMember.OWNER, ChatMember.ADMINISTRATOR})


@dataclass(frozen=True, slots=True)
class Target:
    """Normalized "thing this command should modify".

    `kind='user'` → `obj` is a `User` (DM commands).
    `kind='group'` → `obj` is a `TelegramGroup` (group commands).
    """

    kind: Literal["user", "group"]
    obj: User | TelegramGroup


async def get_or_create_target(session: AsyncSession, update: Update) -> Target:
    """DM → resolve/create a `User` (and its telegram `NotificationChannel`).
    Group → resolve/create a `TelegramGroup` keyed on `chat.id`.

    Handles the idempotency race on UNIQUE constraints — a concurrent /start
    from the same chat raises IntegrityError on the second insert; we catch,
    rollback the failed row, and re-SELECT. End result either way: one row
    exists per chat, and we return it. User-visible behaviour is a plain
    "welcome" message regardless of who won the race.
    """
    chat = update.effective_chat
    tg_user = update.effective_user
    if chat is None or tg_user is None:
        raise ValueError("update missing effective_chat or effective_user")

    if chat.type == Chat.PRIVATE:
        user = await _resolve_or_create_user(session, chat, tg_user)
        return Target(kind="user", obj=user)

    group = await _resolve_or_create_group(session, chat, tg_user)
    return Target(kind="group", obj=group)


async def _resolve_or_create_user(session: AsyncSession, chat: Chat, tg_user: TgUser) -> User:
    """DM flow — look up existing NotificationChannel, else create User +
    Channel atomically."""
    identifier = str(chat.id)

    # Fast path: already registered.
    result = await session.execute(
        select(NotificationChannel).where(
            NotificationChannel.channel_type == ChannelType.TELEGRAM.value,
            NotificationChannel.channel_identifier == identifier,
        )
    )
    channel = result.scalar_one_or_none()
    if channel is not None:
        user = await session.get(User, channel.user_id)
        assert user is not None  # FK invariant
        return user

    # First-time registration — create User with env-driven defaults.
    user = User(
        pref_assets=settings.delivery_default_assets_list,
        pref_min_confidence=settings.delivery_default_min_confidence,
    )
    session.add(user)
    await session.flush()  # assign user.id before we FK to it

    session.add(
        NotificationChannel(
            user_id=user.id,
            channel_type=ChannelType.TELEGRAM.value,
            channel_identifier=identifier,
            username=tg_user.username,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        # Another concurrent /start won the race. Rollback and re-select.
        await session.rollback()
        result = await session.execute(
            select(NotificationChannel).where(
                NotificationChannel.channel_type == ChannelType.TELEGRAM.value,
                NotificationChannel.channel_identifier == identifier,
            )
        )
        channel = result.scalar_one()
        user = await session.get(User, channel.user_id)
        assert user is not None
    return user


async def _resolve_or_create_group(
    session: AsyncSession, chat: Chat, tg_user: TgUser
) -> TelegramGroup:
    result = await session.execute(select(TelegramGroup).where(TelegramGroup.chat_id == chat.id))
    group = result.scalar_one_or_none()
    if group is not None:
        return group

    group = TelegramGroup(
        chat_id=chat.id,
        title=chat.title,
        pref_assets=settings.delivery_default_assets_list,
        pref_min_confidence=settings.delivery_default_min_confidence,
    )
    session.add(group)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        result = await session.execute(
            select(TelegramGroup).where(TelegramGroup.chat_id == chat.id)
        )
        group = result.scalar_one()
    return group


def parse_asset_list(raw: str) -> list[str]:
    """Split + uppercase + validate. Raises ValueError with a user-facing
    message on any invalid asset. Called from /prefs assets handler."""
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("no assets provided")
    invalid = [p for p in parts if p not in _VALID_ASSETS_SET]
    if invalid:
        raise ValueError(
            f"invalid asset(s): {', '.join(invalid)}. "
            f"Supported: {', '.join(sorted(_VALID_ASSETS_SET))}"
        )
    # Dedup while preserving order.
    seen: set[str] = set()
    result: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True iff the message sender is creator/administrator of the group.

    Issue #39 — restricts mutating commands (/prefs, future /subscribe in
    groups) to people who can actually represent the group. Telegram's
    `getChatMember` is the authoritative source: status ∈ {creator,
    administrator} is admin, anything else is not. We don't trust a
    cached/local user-role table because admin status can change at any
    moment in the group itself.

    On API failure (network blip, bot not in chat anymore, etc) we
    **fail closed** — return False rather than risk letting a non-admin
    through. The caller surfaces a generic "only admins" message; the
    underlying error is logged for diagnosis.

    Returns True for DMs as a defensive default — the caller is expected
    to gate this with `chat.type == GROUP` first; if it doesn't, allowing
    the action in DMs is safer than denying it.
    """
    chat = update.effective_chat
    tg_user = update.effective_user
    if chat is None or tg_user is None:
        return False
    if chat.type not in (Chat.GROUP, Chat.SUPERGROUP):
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=chat.id, user_id=tg_user.id)
    except TelegramError as exc:
        log.warning(
            "group_admin_check_failed",
            chat_id=chat.id,
            user_id=tg_user.id,
            error=str(exc),
        )
        return False
    return member.status in _ADMIN_STATUSES


def parse_confidence(raw: str) -> int:
    """Parse + validate 1-10 inclusive. Raises ValueError on anything else."""
    try:
        val = int(raw)
    except ValueError as exc:
        raise ValueError(f"confidence must be an integer, got {raw!r}") from exc
    if not 1 <= val <= 10:
        raise ValueError(f"confidence must be 1-10, got {val}")
    return val
