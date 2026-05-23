"""Pydantic shapes for the PR D.4.2 wallet binding surface.

Four endpoints under `/api/wallet/*`:

  POST /nonce       — anonymous; FE asks for a fresh SIWE nonce.
  POST /verify      — anonymous; FE submits the signed SIWE message.
  GET  /me          — authed (unbound OK); returns current wallet state.
  POST /api-key     — authed (bound required); store per-venue api-key
                      name + sodex account_id.

All address fields are lowercased on write to match the DB CHECK
constraint `^0x[0-9a-f]{40}$` on `User.wallet_address`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from etfpulse.models.order import Venue

# Reusable validator: 0x + 40 hex (any case). Lowercased on write.
_ETH_ADDRESS_PATTERN = r"^0x[0-9a-fA-F]{40}$"


class NonceRequest(BaseModel):
    """`POST /api/wallet/nonce` body."""

    address: str = Field(..., pattern=_ETH_ADDRESS_PATTERN, description="EVM address")


class NonceResponse(BaseModel):
    """Everything the FE needs to construct the EIP-4361 message.

    The FE MUST use these exact values when building the message:
    `domain`, `uri`, `chain_id`, `nonce`, `statement`, `issued_at`. Any
    drift between the response and the eventual `/verify` POST will
    fail at the domain/chain/nonce check in `auth_siwe.consume_and_verify`.
    """

    nonce: str
    statement: str
    domain: str
    uri: str
    chain_id: int
    version: Literal["1"] = "1"
    issued_at: datetime
    expires_at: datetime


class VerifyRequest(BaseModel):
    """`POST /api/wallet/verify` body. The raw EIP-4361 message string
    and the wallet's signature.

    Signature is 0x-prefixed 65-byte hex (132 chars total) per
    EIP-191. The siwe library handles `v ∈ {27, 28}` and `v ∈ {0, 1}`
    forms transparently — no SoDEX `0x01` type prefix here (that
    prefix is SoDEX-gateway-specific; SIWE is plain EIP-191/EIP-1271).
    """

    message: str = Field(..., min_length=1, max_length=4096)
    signature: str = Field(..., pattern=r"^0x[0-9a-fA-F]{130}$")


class VerifyResponse(BaseModel):
    """`POST /api/wallet/verify` 200 body."""

    jwt: str
    user_id: int
    wallet_address: str  # lowercased


class WalletMeResponse(BaseModel):
    """`GET /api/wallet/me` 200 body — snapshot of current user state.

    Drives the FE Execute page's onboarding state-machine:
      - `wallet_address` null   → show "connect wallet" CTA
      - api-key-name null per venue → show "bind SoDEX key" form
      - `paper_trade=True`     → show prominent PAPER badge
    """

    user_id: int
    wallet_address: str | None
    sodex_account_id: int | None
    paper_trade: bool
    sodex_spot_api_key_name: str | None
    sodex_perps_api_key_name: str | None


class SetApiKeyRequest(BaseModel):
    """`POST /api/wallet/api-key` body — store the per-venue named key.

    `api_key_name` is the NAME of the key registered on the SoDEX
    frontend (NOT the EVM address — verified via V.3 capture; see
    CLAUDE.md "SoDEX HTTP adapters" §wire-contract). Validated as a
    short alphanumeric/dash/underscore string to match SoDEX's
    permissive but length-bounded shape.

    `sodex_account_id` is the gateway's numeric accountID — discovered
    by querying `/accounts/{addr}/state` on the venue. Cached on User
    so prepare_new doesn't have to round-trip per order.
    """

    venue: str = Field(..., description="Venue enum value")
    api_key_name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    sodex_account_id: int = Field(..., gt=0)

    @field_validator("venue")
    @classmethod
    def _validate_venue(cls, v: str) -> str:
        # Enum value membership check (StrEnum's `in` doesn't work
        # cleanly across versions; use a frozen value set).
        valid = frozenset(item.value for item in Venue)
        if v not in valid:
            raise ValueError(f"venue must be one of {sorted(valid)}")
        return v


class SetApiKeyResponse(BaseModel):
    """`POST /api/wallet/api-key` 200 body — confirmation only."""

    ok: Literal[True] = True
    venue: str
    api_key_name: str
    sodex_account_id: int


class RequestLiveRequest(BaseModel):
    """`POST /api/wallet/request-live` body (PR #185).

    A paper-trade user submits a request to the operator to be moved
    onto live trading. The operator reviews via Telegram + flips
    `paper_trade=False` via the existing admin route. No automatic
    self-service flip.

    `note` is operator-facing context the user can include (e.g.,
    "first run was successful, ready to go live"). Capped at 500
    chars; the bot message renders it verbatim under HTML-escape, so
    no injection risk.
    """

    note: str | None = Field(default=None, max_length=500)


class RequestLiveResponse(BaseModel):
    """`POST /api/wallet/request-live` 200 body.

    `ok=True` means the operator was notified. The user is still on
    paper-trade until the operator flips them — the response message
    states this explicitly so the user doesn't expect immediate
    behaviour change.
    """

    ok: Literal[True] = True
    message: str
