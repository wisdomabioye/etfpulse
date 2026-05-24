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

import html
import time

import structlog
from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.telegram import TelegramError, telegram_client
from etfpulse.api.auth import (
    JWTError,
    get_current_user,
    get_current_user_unbound,
    mint_jwt,
    verify_jwt,
)
from etfpulse.api.auth_siwe import consume_and_verify, issue_nonce
from etfpulse.api.deps import get_db_session
from etfpulse.api.schemas.wallet import (
    NonceRequest,
    NonceResponse,
    RequestLiveRequest,
    RequestLiveResponse,
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
# POST /verify — anonymous OR authed (Option A — D.5 design pass)
# ---------------------------------------------------------------------------


@router.post("/verify", response_model=VerifyResponse)
async def post_verify(
    body: VerifyRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> VerifyResponse:
    """Validate the SIWE signature, bind wallet to a User, mint JWT.

    Two entry paths, distinguished by inbound Authorization header:

      A. **Anonymous** (no header) — first-time wallet bind from the
         desktop web flow. Find-or-create a User keyed by wallet_address.

      B. **Authed** (`Authorization: Bearer <jwt>` present + valid) —
         the caller already has a session (e.g. Telegram WebApp via
         `auth/telegram/verify`). Bind the verified wallet to THAT
         existing User instead of creating a duplicate.

    Why the two paths land on one route (D.5 design honesty pass):
    a Telegram-WebApp-bound user lands with a JWT and `wallet_address
    IS NULL`. If they ran the anonymous verify path, the backend would
    create a SECOND User keyed by their wallet — splitting their
    identity across two rows. Honoring the inbound JWT collapses both
    use cases through the same route: SIWE proves wallet ownership,
    the existing JWT proves session identity, we UPDATE the User row.

    Race / conflict handling:

      - Concurrent first-binds for the same wallet (path A) collide
        on the partial UNIQUE `ix_users_wallet_address`. The loser
        rolls back + re-SELECTs the winner's row.
      - Path B with a wallet ALREADY bound to a DIFFERENT user → 409.
        We deliberately do NOT silently swap bindings; a wallet
        bound to user X cannot be re-bound to user Y without user X
        being unbound first (operator action, future surface).

    JWT is minted on every success — even on path B (re-mint with
    the same user_id; effectively refreshes the token's `iat`/`exp`).
    """
    address = consume_and_verify(message=body.message, signature=body.signature)

    # Path-disambiguation: parse the inbound JWT (if any) WITHOUT the
    # `wallet_not_bound` gate. The whole point of path B is the caller
    # has a wallet-less session.
    authed_user = await _try_resolve_authed_user(request, session)

    if authed_user is not None:
        user = await _bind_wallet_to_existing_user(
            session, user=authed_user, wallet_address=address
        )
    else:
        user = await _resolve_or_create_user_by_wallet(session, wallet_address=address)

    await session.commit()  # persist new/updated row before minting

    token = mint_jwt(user.id)
    log.info(
        "wallet_verify_ok",
        user_id=user.id,
        address=address,
        path="bind_to_existing" if authed_user is not None else "anonymous_first_bind",
    )
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
# Request live trading (PR #185) — user-facing affordance for the paper-
# trade → live transition. The operator stays the gatekeeper; this route
# just notifies them.
# ---------------------------------------------------------------------------

# In-memory per-user cooldown. Survives the request handler but resets
# on container restart. Operators won't see duplicates within a cooldown
# window; restart-induced double-notifications are tolerated (they cost
# one extra Telegram message). Sized large enough that legitimate user
# counts don't evict each other.
#
# TTL = the MAX possible value of `settings.request_live_cooldown_seconds`
# (per `Field(le=86400)` in config.py). This guarantees the cache retains
# entries for at least as long as the configured cooldown — otherwise an
# operator who raises the cooldown above the cache TTL would see a
# silent gap where entries evict but the handler still wants to enforce.
# The handler reads the LIVE settings value to make the actual decision;
# the cache is just storage.
# MUST match Settings.request_live_cooldown_seconds Field(le=...)
_REQUEST_LIVE_COOLDOWN_MAX_TTL_SECONDS = 86400
_REQUEST_LIVE_COOLDOWN: TTLCache[int, float] = TTLCache(
    maxsize=100_000, ttl=_REQUEST_LIVE_COOLDOWN_MAX_TTL_SECONDS
)


@router.post("/request-live", response_model=RequestLiveResponse)
async def post_request_live(
    body: RequestLiveRequest,
    user: User = Depends(get_current_user),
) -> RequestLiveResponse:
    """Notify the operator that this user wants to be moved to live trading.

    Gate stack (matches code order):
      1. Wallet-authed (`get_current_user` requires a bound wallet —
         paper_trade only matters for users who can place orders).
      2. Per-user cooldown FIRST so a user in cooldown sees the
         "try again in N s" message regardless of whether bot/chat
         config drifted underneath them. Returns 429 with the
         remaining time.
      3. Bot enabled + operator chat configured. If either is empty,
         503 with a clear detail string. Distinct from the webhook's
         404 info-leak policy: this is a user-facing affordance where
         "feature not configured" is the actionable feedback.

    Does NOT flip `paper_trade`. Operator action via
    `POST /api/admin/users/{id}/paper-trade` is the only path that
    actually changes execution behaviour — keeps the safe-by-default
    posture intact.

    `note` is operator-facing context (HTML-escaped before embedding
    in the Telegram message; no injection risk since we render
    `parse_mode=HTML`).
    """
    # Belt: cooldown table refresh — TTLCache reaps lazily on read.
    _REQUEST_LIVE_COOLDOWN.expire()
    last_at = _REQUEST_LIVE_COOLDOWN.get(user.id)
    if last_at is not None:
        elapsed = time.monotonic() - last_at
        if elapsed < settings.request_live_cooldown_seconds:
            remaining = int(settings.request_live_cooldown_seconds - elapsed)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"request_live_cooldown: try again in {remaining}s",
            )

    if not settings.is_bot_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bot not configured — operator contact unavailable",
        )
    if settings.operator_telegram_chat_id == 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operator contact channel not configured",
        )

    # Compose the operator-facing Telegram message. HTML-escape the
    # user-supplied note + wallet address so a future change to the
    # wallet_address format (or a hostile note) can't inject tags.
    note_html = html.escape(body.note or "").strip()
    wallet_html = html.escape(user.wallet_address or "(unbound)")
    note_block = f"\n\n<b>Note:</b> {note_html}" if note_html else ""
    text = (
        "<b>📨 Live-trading request</b>\n"
        f"<b>User:</b> <code>{user.id}</code>\n"
        f"<b>Wallet:</b> <code>{wallet_html}</code>\n"
        f"<b>SoDEX account:</b> <code>{user.sodex_account_id or '—'}</code>\n"
        f"<b>Paper-trade:</b> <code>{user.paper_trade}</code>"
        f"{note_block}\n\n"
        "<i>Flip via `POST /api/admin/users/"
        f'{user.id}/paper-trade` with body <code>{{"paper_trade": false}}</code>.</i>'
    )

    # Reserve the cooldown slot BEFORE the await — closes the race
    # window where two concurrent requests for the same user could
    # both pass the `last_at is None` check, both send a message,
    # both record the cooldown. Without this, an FE bypass (curl
    # spam against the route) could fan out N messages to the
    # operator before the cooldown takes effect; the reservation
    # ensures the FIRST concurrent request "owns" the slot and the
    # rest see it on their cooldown check.
    #
    # On Telegram failure, the slot is RELEASED so the user can
    # retry. Process crash between reservation and send leaks the
    # slot for one cooldown window — acceptable failure mode since
    # the user can ping the operator out-of-band.
    _REQUEST_LIVE_COOLDOWN[user.id] = time.monotonic()
    try:
        await telegram_client.send_message(
            chat_id=settings.operator_telegram_chat_id,
            text=text,
            parse_mode="HTML",
        )
    except TelegramError as exc:
        _REQUEST_LIVE_COOLDOWN.pop(user.id, None)
        # Distinguish transient failure from misconfig so the user UX
        # can show a useful message. We don't try to retry inline —
        # the user can re-submit immediately (no cooldown set).
        log.warning(
            "request_live_send_failed",
            user_id=user.id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="couldn't reach operator right now — please try again later",
        ) from exc

    log.info("request_live_sent", user_id=user.id, note_present=bool(note_html))
    return RequestLiveResponse(
        message=(
            "Request sent. You're still on paper-trade — an operator will "
            "review and reach out to confirm before flipping you to live."
        ),
    )


# ---------------------------------------------------------------------------
# Wallet upsert + JWT-aware bind helpers
# ---------------------------------------------------------------------------
# Three helpers, two paths:
#   - `_resolve_or_create_user_by_wallet` — anonymous path (no JWT).
#     Find-or-create by wallet_address; race-safe via partial UNIQUE.
#   - `_try_resolve_authed_user` — best-effort JWT parse; returns User
#     or None. Used by `/verify` to disambiguate the two paths.
#   - `_bind_wallet_to_existing_user` — authed path; UPDATEs the
#     existing User's wallet_address, with 409 on conflict.


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
    of preference shape. `paper_trade` is initialised from
    `settings.user_paper_trade_default` (True out of the box per PR
    #184 — safe-by-default for mainnet deploys; operators opt users
    INTO live execution via `POST /api/admin/users/{id}/paper-trade`).
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
        paper_trade=settings.user_paper_trade_default,
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


async def _try_resolve_authed_user(request: Request, session: AsyncSession) -> User | None:
    """Best-effort JWT parse: return the User row IF `Authorization` is
    present + valid, else None.

    Used by the `/verify` route to disambiguate the two entry paths:
    anonymous first-bind vs. authed wallet-link-to-existing-user.
    DOES NOT raise on malformed/missing/expired tokens — those just
    drop to the anonymous path, since a bad inbound token shouldn't
    prevent a legitimate first-time SIWE bind from completing.

    `wallet_already_bound` on the existing User is fine — we'll
    re-bind the same address (no-op) or land in the
    `_bind_wallet_to_existing_user` conflict path if the wallet was
    bound to a DIFFERENT user.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token:
        return None
    try:
        claims = verify_jwt(token)
    except JWTError:
        # Treat any bad-token state as "anonymous". A user with a
        # broken-but-non-empty JWT (e.g., expired) still gets a clean
        # first-bind path; their stale token will be replaced by the
        # one we mint on success.
        return None
    user_id = claims["user_id"]
    user = await session.get(User, user_id)
    if user is None:
        # JWT was valid (signature OK, not expired, audience matches) but
        # the User row is gone. Causes: a mint→delete race, manual operator
        # DELETE, DB restore to a snapshot taken before this user existed,
        # OR a long-lived token surviving a user being unbound and re-created
        # under a different id. The call falls through to the anonymous
        # path (correct UX — the SIWE flow rebuilds a user via the wallet
        # address), but log so operators can spot if this fires more than
        # rarely. #78.8.
        log.warning("wallet_verify_authed_user_vanished", user_id=user_id)
        return None
    return user


async def _bind_wallet_to_existing_user(
    session: AsyncSession,
    *,
    user: User,
    wallet_address: str,
) -> User:
    """Set `user.wallet_address = wallet_address` (idempotent re-bind
    of the same address; HTTP 409 on conflict with another User).

    Three real branches:

      - Already bound to THIS user with the SAME wallet → no-op,
        return user unchanged.
      - User has NO wallet → straightforward UPDATE.
      - User has a DIFFERENT wallet OR another User holds this wallet
        → 409. We do not silently overwrite either binding; the
        operator (or a future explicit "unbind" surface) is the only
        sanctioned path to reassign.
    """
    if user.wallet_address == wallet_address:
        # Idempotent re-bind. Useful when the FE retries the verify
        # call after a network blip + the user re-signs.
        return user

    if user.wallet_address is not None:
        # User is already wallet-bound to a DIFFERENT address. We
        # don't silently swap (would erase the binding the user
        # already approved). 409 surfaces the conflict cleanly.
        log.warning(
            "wallet_bind_existing_user_already_has_wallet",
            user_id=user.id,
            existing=user.wallet_address,
            attempted=wallet_address,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="wallet_swap_not_allowed",
        )

    # Check the address isn't already bound to ANOTHER user (partial
    # UNIQUE on User.wallet_address would catch this at flush, but
    # surfacing it as 409 with a clear detail is friendlier than
    # bubbling IntegrityError → 500.
    result = await session.execute(
        select(User).where(User.wallet_address == wallet_address, User.id != user.id)
    )
    conflict = result.scalar_one_or_none()
    if conflict is not None:
        log.warning(
            "wallet_bind_address_already_bound",
            attempting_user_id=user.id,
            owning_user_id=conflict.id,
            wallet=wallet_address,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="wallet_already_bound_to_other_user",
        )

    # All clear — UPDATE the existing user with the verified wallet.
    user.wallet_address = wallet_address
    try:
        await session.flush()
    except IntegrityError as exc:
        # Race: between our SELECT and our UPDATE, another transaction
        # claimed the wallet. Surface as 409 (same shape as the
        # check-above path).
        await session.rollback()
        log.warning(
            "wallet_bind_race_lost",
            user_id=user.id,
            wallet=wallet_address,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="wallet_already_bound_to_other_user",
        ) from exc
    return user
