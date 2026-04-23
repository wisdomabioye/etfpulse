"""Shared helpers for bot command handlers.

The DM-vs-group branching + target lookup is the cross-cutting concern — we
extract it so each handler stays readable. Anti-drift note: these helpers
are INTERNAL to the handlers package (leading underscore in the filename
conveys "not public API"); don't import from them outside `bot/handlers/*`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Chat, Update
from telegram import User as TgUser

from etfpulse.config import settings
from etfpulse.models import ChannelType, NotificationChannel, TelegramGroup, User

# Assets we accept in /prefs. Matches what detectors emit and what the
# ingestion adapter pulls for.
_VALID_ASSETS = {"BTC", "ETH"}


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
    invalid = [p for p in parts if p not in _VALID_ASSETS]
    if invalid:
        raise ValueError(
            f"invalid asset(s): {', '.join(invalid)}. Supported: {', '.join(sorted(_VALID_ASSETS))}"
        )
    # Dedup while preserving order.
    seen: set[str] = set()
    result: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def parse_confidence(raw: str) -> int:
    """Parse + validate 1-10 inclusive. Raises ValueError on anything else."""
    try:
        val = int(raw)
    except ValueError as exc:
        raise ValueError(f"confidence must be an integer, got {raw!r}") from exc
    if not 1 <= val <= 10:
        raise ValueError(f"confidence must be 1-10, got {val}")
    return val
