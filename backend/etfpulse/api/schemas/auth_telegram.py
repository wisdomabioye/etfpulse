"""Pydantic shapes for `/api/auth/telegram/verify` (PR D.5.1)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TelegramVerifyRequest(BaseModel):
    """`POST /api/auth/telegram/verify` body.

    `init_data` is the raw query-string Telegram emits to the WebApp.
    The FE pulls it from `window.Telegram.WebApp.initData` and forwards
    verbatim — NO pre-decoding (the HMAC is computed against the
    URL-encoded form per Telegram's spec).
    """

    init_data: str = Field(..., min_length=1, max_length=8192)


class TelegramVerifyResponse(BaseModel):
    """`POST /api/auth/telegram/verify` 200 body."""

    jwt: str
    user_id: int
    telegram_user_id: int
    has_wallet: bool
