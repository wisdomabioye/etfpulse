"""Shared `User` upsert helpers — Telegram + (future) other identity sources.

Why this module exists: the bot's DM `/start` path (`bot/handlers/_common.py`)
and the D.5 Telegram WebApp verify route both need to resolve-or-create a
`User` row keyed by Telegram user id. Without a shared helper, the two
write sites would drift — and the convergence guarantee (DM `chat.id ==
user.id` so both paths upsert the same NotificationChannel row) would be
fragile to one site changing its query shape independently.

The helpers live OUTSIDE the bot package because the WebApp verify route
lives in `api/`, and the `api` layer must never import from `bot/` (the
HTTP-vs-bot layering doesn't pull bot deps into the request path).
Conversely, `bot/handlers/_common.py` is the place where the bot DM flow
imports from THIS module — making `identity.py` the single owner of the
User-upsert-by-Telegram-id invariant.

Race-safe via `IntegrityError` on the partial UNIQUE index
`ix_channels_unique (channel_type, channel_identifier)` — concurrent
first-binds collide; the loser rolls back and re-SELECTs.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.config import settings
from etfpulse.models import ChannelType, NotificationChannel, User


async def resolve_or_create_user_by_tg_id(
    session: AsyncSession,
    *,
    tg_user_id: int,
    username: str | None,
) -> User:
    """Return the User row bound to Telegram user id `tg_user_id`.

    Identity contract: Telegram identity is keyed by the user's Telegram
    id (NOT a column on `User` — kept on `NotificationChannel` so the
    same User can have multiple delivery channels later). For DM chats,
    Telegram protocol guarantees `chat.id == user.id`, so this helper
    matches both the bot's `/start` upsert (which historically passed
    `chat.id`) and the D.5 WebApp verifier (which passes `initData.user.id`).

    Race-safe: concurrent first-binds for the same id collide on the
    partial UNIQUE index `ix_channels_unique`. The loser catches
    `IntegrityError`, rolls back, and re-SELECTs the winner's row.

    `username` is stored verbatim on the new NotificationChannel for
    operator-side support context. Pre-existing channels keep their
    original username (we don't UPDATE on hit) — username churn is
    common and not load-bearing.
    """
    identifier = str(tg_user_id)

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
            username=username,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        # Another concurrent caller (bot /start OR another WebApp verify)
        # won the race. Roll back the conflicting insert + re-SELECT.
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
