"""Wallet binding routes (PR D.4.2).

Four endpoints implementing the SIWE-based wallet authentication flow:

  POST /api/wallet/nonce   → issue single-use nonce + everything the FE
                              needs to construct the SIWE message.
  POST /api/wallet/verify  → consume nonce, verify signature, upsert
                              User, mint JWT, return.
  GET  /api/wallet/me      → authed (unbound OK); current user state.
  POST /api/wallet/api-key → authed (wallet-bound); store per-venue
                              SoDEX API key name + account_id.

Concurrency model on wallet upsert: the `User.wallet_address` partial
UNIQUE index (`ix_users_wallet_address WHERE wallet_address IS NOT NULL`)
collapses concurrent first-bind races to one winner. The loser gets
IntegrityError → we rollback and re-SELECT the winning row. Same
shape as `bot/handlers/_common.py:_resolve_or_create_user` for tg-id
binding, intentionally — D.5 will share this pattern once a `etfpulse/
identity.py` helper module is factored.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.auth import get_current_user, get_current_user_unbound, mint_jwt
from etfpulse.api.auth_siwe import consume_and_verify, issue_nonce
from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.wallet import (
    NonceRequest,
    NonceResponse,
    SetApiKeyRequest,
    SetApiKeyResponse,
    VerifyRequest,
    VerifyResponse,
    WalletMeResponse,
)
from etfpulse.config import settings
from etfpulse.models.order import Venue
from etfpulse.models.user import User

log = structlog.get_logger()
router = APIRouter(prefix="/wallet", tags=["wallet"])


# ---------------------------------------------------------------------------
# POST /nonce — anonymous
# ---------------------------------------------------------------------------


@router.post("/nonce", response_model=NonceResponse)
async def post_nonce(body: NonceRequest) -> NonceResponse:
    """Issue a single-use SIWE nonce for `body.address`.

    The response gives the FE every field it needs to construct the
    EIP-4361 message (domain, uri, chain_id, statement, issued_at,
    expires_at) so there's no drift between the nonce request and the
    eventual `/verify` POST.

    Nonce is single-use AND TTL-bounded. Issuing multiple nonces for
    the same address is allowed (independent tabs) — each independently
    consumable.

    No auth — anyone with an address can request a nonce; the
    signature on `/verify` proves wallet ownership.
    """
    if not settings.siwe_domain:
        # Server misconfig — same check `auth_siwe.consume_and_verify`
        # does, surfaced earlier here so the FE doesn't waste a wallet
        # prompt cycle on an unworkable backend.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="server: siwe domain not configured",
        )

    nonce, issued_at, expires_at = issue_nonce(body.address)
    return NonceResponse(
        nonce=nonce,
        statement=settings.siwe_statement,
        domain=settings.siwe_domain,
        uri=settings.frontend_url,
        chain_id=settings.sodex_chain_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# POST /verify — anonymous
# ---------------------------------------------------------------------------


@router.post("/verify", response_model=VerifyResponse)
async def post_verify(
    body: VerifyRequest,
    session: AsyncSession = Depends(get_db_session),
) -> VerifyResponse:
    """Validate the SIWE signature, upsert the User by wallet, mint JWT.

    `consume_and_verify` does the full SIWE chain (parse, domain,
    chain_id, nonce, address-binding, signature, single-use consume)
    and returns the lowercased recovered address. We then either find
    an existing User by that address or create a new one.

    Race handling: concurrent first-binds for the same address
    collide on `ix_users_wallet_address` partial UNIQUE. The loser
    rolls back and re-SELECTs the winner's row. Both paths return a
    valid User; identical UX.

    JWT is minted with default audience (`execution`). FE stores it
    and rides it on every subsequent authed call.
    """
    address = consume_and_verify(message=body.message, signature=body.signature)

    user = await _resolve_or_create_user_by_wallet(session, wallet_address=address)
    await session.commit()  # persist new User row before minting

    token = mint_jwt(user.id)
    log.info("wallet_verify_ok", user_id=user.id, address=address)
    return VerifyResponse(jwt=token, user_id=user.id, wallet_address=address)


# ---------------------------------------------------------------------------
# GET /me — authed (wallet may be unbound for this introspection path)
# ---------------------------------------------------------------------------


@router.get("/me", response_model=WalletMeResponse)
async def get_me(user: User = Depends(get_current_user_unbound)) -> WalletMeResponse:
    """Return current user snapshot for FE onboarding state.

    Uses `get_current_user_unbound` so a freshly-minted JWT for a
    wallet-less user (e.g., D.5 Telegram-WebApp path before SIWE
    bind) can still read its own state to render the onboarding UI.
    The `paper_trade` flag is operator-controlled (admin route), not
    user-mutable here.
    """
    return WalletMeResponse(
        user_id=user.id,
        wallet_address=user.wallet_address,
        sodex_account_id=user.sodex_account_id,
        paper_trade=user.paper_trade,
        sodex_spot_api_key_name=user.sodex_spot_api_key_name,
        sodex_perps_api_key_name=user.sodex_perps_api_key_name,
    )


# ---------------------------------------------------------------------------
# POST /api-key — authed (wallet must be bound)
# ---------------------------------------------------------------------------


@router.post("/api-key", response_model=SetApiKeyResponse)
async def post_api_key(
    body: SetApiKeyRequest,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> SetApiKeyResponse:
    """Store the per-venue SoDEX API key name + sodex account_id.

    Wallet must be bound (get_current_user enforces). User runs this
    once per venue after registering the named API key on the SoDEX
    frontend. Subsequent runs overwrite — useful for key rotation.

    `sodex_account_id` is single-valued on User: setting it via the
    spot venue and again via perps with a different account would
    be a misconfiguration (a wallet has one accountID across venues
    per V.3 capture). We don't enforce same-id-across-venues here
    because the API isn't venue-scoped on that column; if operators
    mismatch, the gateway will reject signed writes downstream and
    surface the issue.

    FOR UPDATE on the User row prevents a concurrent admin paper-trade
    flip from getting interleaved with a key-name write — both writers
    queue on the same lock.
    """
    # Re-lock the user row (we hold a fresh ref from the dep, but the
    # row may have been mutated since). Anti-drift rule 30 applies to
    # execution writes; wallet binding writes are independent of that
    # rule but reuse the same primitive for consistency.
    locked = await session.execute(select(User).where(User.id == user.id).with_for_update())
    user = locked.scalar_one()

    user.sodex_account_id = body.sodex_account_id
    if body.venue == Venue.SODEX_SPOT.value:
        user.sodex_spot_api_key_name = body.api_key_name
    elif body.venue == Venue.SODEX_PERPS.value:
        user.sodex_perps_api_key_name = body.api_key_name
    else:
        # Schema validator already gates this; defensive belt.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown venue {body.venue!r}",
        )

    await session.commit()
    log.info(
        "wallet_api_key_set",
        user_id=user.id,
        venue=body.venue,
        api_key_name=body.api_key_name,
        sodex_account_id=body.sodex_account_id,
    )
    return SetApiKeyResponse(
        venue=body.venue,
        api_key_name=body.api_key_name,
        sodex_account_id=body.sodex_account_id,
    )


# ---------------------------------------------------------------------------
# Wallet-by-address upsert
# ---------------------------------------------------------------------------


async def _resolve_or_create_user_by_wallet(
    session: AsyncSession,
    *,
    wallet_address: str,
) -> User:
    """Find an existing User by lowercased wallet_address, else create one.

    The `User.wallet_address` partial UNIQUE index makes the create
    branch race-safe — concurrent first-binds for the same wallet
    collide on the index; the loser rolls back and re-SELECTs.

    New users are created with the env-driven delivery defaults so
    they're indistinguishable from a Telegram-bound new user in terms
    of preference shape. `paper_trade` defaults to False (per User
    model definition) — execution-route policy gates whether real
    funds can move, not this binding step.
    """
    if not wallet_address.startswith("0x") or wallet_address != wallet_address.lower():
        # Defensive — `consume_and_verify` lowercases its return, but if
        # a future caller passes an unnormalised value we want to fail
        # loudly rather than violate the DB CHECK constraint.
        raise ValueError(f"wallet_address must be lowercased 0x... format, got {wallet_address!r}")

    result = await session.execute(select(User).where(User.wallet_address == wallet_address))
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    # Create. Defaults come from User model + DeliveryPrefsMixin —
    # pref_assets, pref_min_confidence, pref_paused, etc. all have
    # sane defaults.
    user = User(
        wallet_address=wallet_address,
        pref_assets=settings.delivery_default_assets_list,
        pref_min_confidence=settings.delivery_default_min_confidence,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Concurrent insert won the race. Rollback the failed insert
        # and re-SELECT the winner.
        await session.rollback()
        result = await session.execute(select(User).where(User.wallet_address == wallet_address))
        user = result.scalar_one()
    return user
