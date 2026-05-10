"""Admin-only DTOs for Telegram webhook management."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RotateWebhookSecretRequest(BaseModel):
    """Optional body for `POST /api/admin/telegram/rotate-webhook-secret`.

    `secret` lets the operator supply a pre-generated value (useful when
    they want to update Coolify env at the same time without round-tripping
    through the response). If omitted, the server generates a fresh
    `secrets.token_hex(32)` (256 bits, hex-encoded; matches what
    `openssl rand -hex 32` produces — same shape as the boot value).
    """

    secret: str | None = Field(
        default=None,
        min_length=32,
        max_length=256,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Optional operator-supplied secret. Must match Telegram's allowed "
            "alphabet: A-Z, a-z, 0-9, underscore, hyphen; 1-256 chars. "
            "We enforce >= 32 chars for entropy."
        ),
    )


class RotateWebhookSecretResponse(BaseModel):
    """One-time disclosure of the newly active secret.

    `note` is a reminder that the env var (TELEGRAM_WEBHOOK_SECRET) is now
    stale and must be updated for the rotation to survive a container
    restart. We can't update Coolify env from inside the container, so
    this is the operator's job.
    """

    secret: str
    note: str = (
        "Rotation complete. Update TELEGRAM_WEBHOOK_SECRET in your deploy "
        "environment now — without it, a container restart will re-register "
        "the OLD secret with Telegram and break webhook delivery."
    )
