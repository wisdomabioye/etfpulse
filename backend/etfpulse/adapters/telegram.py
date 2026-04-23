"""Telegram adapter — thin wrapper over python-telegram-bot's `Bot`.

Wraps PTB's exception hierarchy in our own (`TelegramError`,
`TelegramBlockedError`, `TelegramChatNotFoundError`) so callers don't import
from `telegram.error`. Same pattern as `SoSoValueError` vs httpx errors —
keeps adapter swap-out cheap.

NOTE on the name collision: `telegram.error.TelegramError` (PTB) and
`etfpulse.adapters.telegram.TelegramError` (ours) are different classes.
Pipeline code catches OURS. PTB's stays internal to this module.

Methods:
    `send_message(chat_id, text, parse_mode='HTML') -> SentMessage` — raises if
        token unset (loud failure preferred over silent dropped alert).
    `set_webhook(url, secret_token, allowed_updates) -> bool` — idempotent;
        returns True if it actually called Telegram, False if no drift.
    `get_webhook_info() -> dict` — current registered webhook state.
    `delete_webhook()` — for tear-down / clean-room dev work.

Webhook methods are no-op (warning logged) when token is unset, since they
fire from startup paths where forgiving behaviour is appropriate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from telegram import Bot
from telegram.error import BadRequest, Forbidden
from telegram.error import TelegramError as PTBTelegramError

from etfpulse.config import settings

log = structlog.get_logger()


# Telegram returns "Forbidden: bot was blocked by the user" or similar via
# Forbidden, and "Bad Request: chat not found" via BadRequest. We pattern-
# match the BadRequest message because that's the only signal — there's no
# distinct exception class for chat-not-found.
_CHAT_NOT_FOUND_PATTERNS = (
    "chat not found",
    "chat_not_found",
    "chat is not accessible",
)


# ---------------------------------------------------------------------------
# Exceptions — our own hierarchy. Callers import from this module.
# ---------------------------------------------------------------------------


class TelegramError(Exception):
    """Base class for adapter errors."""


class TelegramBlockedError(TelegramError):
    """User blocked the bot OR bot was kicked from a group (HTTP 403).

    Caller (send worker) marks the relevant `NotificationChannel` /
    `TelegramGroup` as `is_active=false` so we don't keep retrying. No retry
    policy per Decision D4.
    """


class TelegramChatNotFoundError(TelegramError):
    """Telegram doesn't recognize the chat_id (HTTP 400 with "chat not found").

    Same handling as `TelegramBlockedError` from the send worker's POV —
    deactivate the channel/group.
    """


# ---------------------------------------------------------------------------
# Send result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Lightweight return value — just enough for `SignalDelivery.sent_at`
    bookkeeping. Avoids leaking the full PTB `Message` object into pipeline
    code (which would couple us to PTB's API surface).
    """

    message_id: int
    chat_id: int
    sent_at: datetime


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TelegramClient:
    """Module singleton-friendly wrapper. PTB's `Bot` is cached and
    invalidated when `settings.telegram_bot_token` changes (test monkeypatch
    pattern). Construction is cheap — Bot doesn't open connections until first
    HTTP call."""

    def __init__(self) -> None:
        self._bot_cache: Bot | None = None

    @property
    def token(self) -> str:
        return settings.telegram_bot_token

    def _bot(self) -> Bot:
        # Invalidate when token changes — tests monkeypatch settings between
        # cases, so a cached Bot(old_token) would silently use stale auth.
        if self._bot_cache is None or self._bot_cache.token != self.token:
            self._bot_cache = Bot(self.token)
        return self._bot_cache

    # --- Send ---------------------------------------------------------------

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "HTML",
    ) -> SentMessage:
        """Send one message. Raises on any failure — no internal retry (D4)."""
        if not self.token:
            raise TelegramError("telegram_bot_token not configured")

        try:
            msg = await self._bot().send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        except Forbidden as exc:
            log.warning("telegram_send_blocked", chat_id=chat_id, error=str(exc))
            raise TelegramBlockedError(str(exc)) from exc
        except BadRequest as exc:
            msg_lower = str(exc).lower()
            if any(p in msg_lower for p in _CHAT_NOT_FOUND_PATTERNS):
                log.warning(
                    "telegram_send_chat_not_found",
                    chat_id=chat_id,
                    error=str(exc),
                )
                raise TelegramChatNotFoundError(str(exc)) from exc
            log.warning("telegram_send_bad_request", chat_id=chat_id, error=str(exc))
            raise TelegramError(str(exc)) from exc
        except PTBTelegramError as exc:
            log.warning("telegram_send_failed", chat_id=chat_id, error=str(exc))
            raise TelegramError(str(exc)) from exc

        return SentMessage(
            message_id=msg.message_id,
            chat_id=msg.chat.id,
            sent_at=datetime.now(UTC),
        )

    # --- Webhook management ------------------------------------------------

    async def get_webhook_info(self) -> dict:
        """Returns the current webhook config as a plain dict (drops the PTB
        `WebhookInfo` wrapper). Empty dict if token is unset."""
        if not self.token:
            log.warning("telegram_get_webhook_no_token")
            return {}
        info = await self._bot().get_webhook_info()
        return info.to_dict()

    async def set_webhook(
        self,
        url: str,
        secret_token: str,
        allowed_updates: list[str],
    ) -> bool:
        """Always pushes config to Telegram on every call. Returns True on
        success, False if no-op (token unset).

        We deliberately do NOT check `getWebhookInfo` first to skip the call.
        Reasons:
        1. Telegram's getWebhookInfo does NOT return the stored secret_token
           (write-only by API design), so we couldn't detect secret rotation
           even if we wanted to.
        2. setWebhook is idempotent on Telegram's side — calling with
           identical params is a no-op for them.
        3. Bot startup runs once per container deploy (rare). One extra HTTP
           call per boot is negligible.

        The previous "skip if URL+allowed_updates match" design saved nothing
        meaningful and broke secret rotation. Always-set is the simpler and
        more correct contract.
        """
        if not self.token:
            log.warning("telegram_set_webhook_no_token", url=url)
            return False

        await self._bot().set_webhook(
            url=url,
            secret_token=secret_token,
            allowed_updates=allowed_updates,
        )
        log.info(
            "telegram_webhook_set",
            url=url,
            allowed_updates=sorted(allowed_updates),
        )
        return True

    async def delete_webhook(self) -> None:
        """Clear the webhook registration. No-op if token is unset."""
        if not self.token:
            log.warning("telegram_delete_webhook_no_token")
            return
        await self._bot().delete_webhook()
        log.info("telegram_webhook_deleted")


# Module singleton. Tests that need fresh state should instantiate
# TelegramClient() directly rather than using this.
telegram_client = TelegramClient()
