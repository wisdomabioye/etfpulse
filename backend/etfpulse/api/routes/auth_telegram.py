"""Telegram WebApp verify route (PR D.5.1).

Single endpoint:

  POST /api/auth/telegram/verify  — anonymous; FE submits raw initData;
                                     backend verifies HMAC, upserts
                                     User by tg_user_id, mints JWT.

Anonymous: no Authorization header expected. The route IS the bootstrap
that mints the first JWT for a Telegram WebApp visitor.

Bot-disabled posture: returns 404 (NOT 503) when `is_bot_enabled` is
False. Matches `/api/telegram/webhook/*` info-leak policy — scanners
can't distinguish "endpoint doesn't exist" from "bot is off."

Bot token rotation: the verifier reads `settings.telegram_bot_token`
per-request (not cached). A hot rotation invalidates in-flight initData
(captured under the old token) — the user re-launches the WebApp to get
a fresh payload signed with the new token. Acceptable failure mode.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.auth import mint_jwt
from etfpulse.api.auth_telegram import verify_webapp_init_data
from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.auth_telegram import (
    TelegramVerifyRequest,
    TelegramVerifyResponse,
)
from etfpulse.config import settings
from etfpulse.identity import resolve_or_create_user_by_tg_id

log = structlog.get_logger()
router = APIRouter(prefix="/auth/telegram", tags=["auth"])


@router.post("/verify", response_model=TelegramVerifyResponse)
async def post_telegram_verify(
    body: TelegramVerifyRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TelegramVerifyResponse:
    """Validate initData HMAC, upsert User by tg_user_id, mint JWT.

    On verify failure → `WebAppVerifyError` (400) propagated by the
    verifier with a specific detail string (`"invalid hash"`,
    `"init_data expired"`, etc).

    On success → upsert User via the shared `resolve_or_create_user_by_tg_id`
    helper (same path as the bot's DM `/start` flow — DM `chat.id ==
    tg_user.id` per Telegram protocol, so both write sites converge on
    the same `NotificationChannel` row).

    Bot-disabled deployments return 404 (not 503) so scanners can't
    distinguish "no such endpoint" from "off"; matches the existing
    `/api/telegram/webhook/*` policy.
    """
    if not settings.is_bot_enabled:
        # Mirror the webhook info-leak policy. Bot disabled → route
        # doesn't exist as far as a scanner can tell.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # Read bot_token per-request (not cached) — survives a hot rotation
    # via admin-secret-rotate without restarting the verifier path.
    bot_token = settings.telegram_bot_token

    user_data = verify_webapp_init_data(
        body.init_data,
        bot_token=bot_token,
        max_age_seconds=settings.webapp_init_data_max_age_seconds,
    )

    tg_user_id: int = user_data["id"]
    username = user_data.get("username")
    # Telegram usernames are 1-32 chars, alphanumeric + underscore. We
    # accept whatever Telegram sent without re-validation — the field
    # is operator-debug context, not load-bearing.

    user = await resolve_or_create_user_by_tg_id(
        session,
        tg_user_id=tg_user_id,
        username=username,
    )
    await session.commit()

    token = mint_jwt(user.id)
    log.info(
        "telegram_verify_ok",
        user_id=user.id,
        tg_user_id=tg_user_id,
        has_wallet=user.wallet_address is not None,
    )
    return TelegramVerifyResponse(
        jwt=token,
        user_id=user.id,
        telegram_user_id=tg_user_id,
        has_wallet=user.wallet_address is not None,
    )
