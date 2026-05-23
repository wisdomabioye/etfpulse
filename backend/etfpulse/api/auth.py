"""JWT mint/verify + `get_current_user` dependency.

Foundation for the PR D.4 execution surface and PR D.5 Telegram WebApp
entry. Two callers issue tokens:

  - `POST /api/wallet/verify` (D.4) — after SIWE signature validation.
  - `POST /api/auth/telegram/verify` (D.5) — after WebApp initData HMAC.

Tokens then ride on `Authorization: Bearer <jwt>` for every authed call.
This module is the single source of truth for what a session token
looks like — both mint paths route through `mint_jwt`, every authed
route routes through `get_current_user`.

Algorithm pin: HS256. The `algorithms` parameter on `jwt.decode` MUST
be a single-element iterable containing only `"HS256"` — `pyjwt`
otherwise accepts whatever the header claims, which is the textbook
alg-confusion vulnerability (token signed with `alg=none` accepted as
authentic). The pin lives in `_JWT_ALGORITHMS`, declared as a tuple
(immutable) so an accidental `.append(...)` can't silently widen it.

Audience pin: `aud='execution'`. Forward-compat seam — when a separate
"analytics-read" JWT class lands later, that surface will mint with a
different audience and `get_current_user` will keep rejecting the
wrong-aud token automatically.

Clock skew: 30s leeway on `iat`/`exp`. Wallet sign + verify happens
across two clocks (user device + server); a tighter window would
reject legitimate sessions on a slightly fast/slow wall clock.

Secret management:
  - Prod: `settings.jwt_secret` MUST be set; the preflight in
    `api/config_check.py` hard-errors when empty.
  - Dev: empty secret triggers a one-shot ephemeral generation at first
    `mint_jwt` call, logged at WARNING level. Process-local — restart
    invalidates every issued token. Acceptable for host-native dev.

No signing primitives live here. SIWE signature recovery (D.4.2) and
Telegram WebApp HMAC verification (D.5.1) live in their own modules
(`auth_siwe.py`, `auth_telegram.py`). `api/auth.py` is the symmetric-
crypto layer only — JWT mint/verify and the FastAPI dependency.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import jwt
import structlog
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session
from etfpulse.config import settings
from etfpulse.models.user import User

log = structlog.get_logger(__name__)

# Single-element tuple (intentionally immutable). `pyjwt` rejects any
# token whose header claims a different alg — including the `none`
# alg-confusion attack. A mutable list here would be a foot-gun: an
# accidental `_JWT_ALGORITHMS.append("none")` anywhere in the process
# would silently re-introduce the vulnerability we defended against.
# `pyjwt.decode` accepts any iterable for `algorithms`, so the tuple
# form drops in without other changes.
_JWT_ALGORITHMS: tuple[str, ...] = ("HS256",)

# Audience for the execution surface. A future analytics-read JWT would
# mint with `aud='analytics'`; `verify_jwt(audience='execution')` rejects
# the wrong-aud token even with a valid signature.
_AUDIENCE_EXECUTION = "execution"

# Clock skew tolerance on iat/exp. 30 seconds covers reasonable device
# clock drift without opening a meaningful replay window.
_LEEWAY_SECONDS = 30

# Process-local ephemeral secret for dev when `settings.jwt_secret` is
# empty. Generated lazily on first mint; logged once. Production
# preflight hard-errors before this branch ever fires.
#
# Concurrency note: the lazy-init below is NOT mutex-guarded. Safe in
# our model because the FastAPI server runs a single asyncio event loop
# per process and there is no await between the `is None` check and the
# write — no preemption point. Multi-worker uvicorn (`workers=2`) runs
# separate processes; tokens minted by worker A would fail validation
# on worker B, which is the documented "process-local" trade-off.
_ephemeral_secret: str | None = None


class JWTError(HTTPException):
    """401 raised for any JWT-auth failure.

    Detail-string policy (intentional — three distinguishable categories
    map to three FE behaviours):

      - ``"missing bearer token"`` — no/malformed Authorization header.
        FE shows "please log in" (the user hasn't started a session).
      - ``"token expired"`` — `exp < now - leeway`. FE shows "your
        session has expired" and re-launches the bind flow.
      - ``"invalid token"`` — cryptographic-failure catch-all (wrong
        signature, wrong audience, wrong algorithm, malformed payload,
        bad `sub` claim, vanished user). FE shows a generic auth error.

    The cryptographic-failure modes are deliberately collapsed into a
    single string so a probing client can't distinguish 'wrong sig'
    from 'wrong aud' from 'malformed JSON' — that distinction belongs
    in server logs (each `verify_jwt` branch logs a distinct event),
    not in the response body.
    """

    def __init__(self, detail: str = "invalid token") -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _resolve_secret() -> str:
    """Return the active signing secret.

    Prod path: `settings.jwt_secret` non-empty (preflight enforced).
    Dev path: generate-on-first-use ephemeral secret; log a WARNING the
    first time so the operator notices. Process-local — restart
    invalidates issued tokens, which is the price of zero-config dev.
    """
    if settings.jwt_secret:
        return settings.jwt_secret
    global _ephemeral_secret
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_urlsafe(48)
        log.warning(
            "jwt_secret_ephemeral_generated",
            note=(
                "JWT_SECRET unset — using a process-local ephemeral secret. "
                "Tokens are invalidated on every restart. Set JWT_SECRET in "
                "production (preflight will hard-error)."
            ),
        )
    return _ephemeral_secret


def mint_jwt(
    user_id: int,
    *,
    audience: str = _AUDIENCE_EXECUTION,
    ttl_seconds: int | None = None,
) -> str:
    """Issue a session token for `user_id`.

    Claims:
      - `sub`: stringified user_id (RFC 7519 §4.1.2 — `sub` is a string)
      - `aud`: audience (default 'execution')
      - `iat`: issued-at (UTC unix-seconds)
      - `exp`: iat + effective TTL (see `ttl_seconds` below)
      - `jti`: random token id (forward-compat for a future blocklist)

    `sub` MUST be a string per spec — `pyjwt` accepts an int but some
    third-party verifiers don't, and a stringly-typed `sub` future-
    proofs the wire format for tooling.

    `user_id` must be > 0. A 0/negative would round-trip the wire (the
    verifier would parse `int(sub)`) but `session.get(User, 0)` always
    returns None → forever-401. Failing loud at the mint site catches
    the programmer-error case at the moment of the bug rather than at
    the user's next request.

    `ttl_seconds` (#78.9) — per-call override for the token lifetime.
    `None` (default) uses `settings.jwt_ttl_seconds` (24h default — the
    SIWE path). The Telegram WebApp path passes
    `settings.webapp_jwt_ttl_seconds` (1h default) since re-launching
    the WebApp is cheap, so a tighter blast-radius is appropriate.
    Validated > 0 — a zero or negative override would mint a token
    that's already expired at iat (or in the past), with no recovery
    path for the caller.
    """
    if user_id <= 0:
        raise ValueError(f"mint_jwt: user_id must be > 0, got {user_id!r}")
    if ttl_seconds is not None and ttl_seconds <= 0:
        raise ValueError(f"mint_jwt: ttl_seconds must be > 0, got {ttl_seconds!r}")
    effective_ttl = ttl_seconds if ttl_seconds is not None else settings.jwt_ttl_seconds
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=effective_ttl)).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, _resolve_secret(), algorithm=_JWT_ALGORITHMS[0])


def verify_jwt(token: str, *, audience: str = _AUDIENCE_EXECUTION) -> dict:
    """Decode + validate a token; return claims dict on success.

    On success, the returned dict carries the standard JWT claims plus
    an injected `user_id: int` materialised from `sub`. Mutating the
    returned dict is fine — pyjwt returns a fresh dict per call.

    Raises `JWTError` on:
      - Expired (`exp < now - leeway`) → detail ``"token expired"``
      - Signature mismatch, audience mismatch, algorithm mismatch,
        malformed token, missing required claim, or `sub` not a
        string of decimal digits → detail ``"invalid token"``

    The expired case is the only branch with a distinguishable detail
    string (FE shows "session expired" specifically); all cryptographic-
    failure modes collapse to one body string to avoid information
    disclosure. Each branch logs a distinct structlog event so the
    server-side audit trail keeps the category. See `JWTError` for the
    full detail-string policy.
    """
    try:
        claims = jwt.decode(
            token,
            _resolve_secret(),
            algorithms=_JWT_ALGORITHMS,
            audience=audience,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["sub", "aud", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        log.info("jwt_verify_expired", error=str(exc))
        raise JWTError("token expired") from exc
    except jwt.InvalidAudienceError as exc:
        log.info("jwt_verify_wrong_audience", error=str(exc))
        raise JWTError("invalid token") from exc
    except jwt.InvalidAlgorithmError as exc:
        # Header claims an alg we don't accept. The `none`-alg attack
        # lands here when the verifier permissively trusts header alg.
        log.warning("jwt_verify_wrong_algorithm", error=str(exc))
        raise JWTError("invalid token") from exc
    except jwt.PyJWTError as exc:
        # Catch-all for the rest: signature, malformed, missing claims.
        log.info("jwt_verify_failed", error=str(exc), error_type=type(exc).__name__)
        raise JWTError("invalid token") from exc

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.isdigit():
        log.info("jwt_verify_bad_sub", sub_type=type(sub).__name__)
        raise JWTError("invalid token")
    # Materialise the user_id for callers — saves them re-parsing.
    claims["user_id"] = int(sub)
    return claims


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Resolve the bearer token to a User row.

    401 chain:
      - Missing Authorization header, or scheme is not `bearer`
        (case-insensitive per RFC 7235 §2.1)
      - Token verification failure (any cause — see `verify_jwt`)
      - User row vanished (deleted between mint and call)

    403 chain (separated so the FE can distinguish "log in again" from
    "complete onboarding"):
      - User.wallet_address is NULL — D.4 routes require a bound wallet
        before any execution call. SIWE binds it; Telegram-only users
        without a wallet hit this gate.

    Callers that need a User WITHOUT the wallet-bound requirement
    (e.g., the `POST /api/wallet/api-key` route — wallet must be bound
    BUT a different sub-set of routes might also want to be reached
    before wallet is set) use `get_current_user_unbound` below.
    """
    user = await _resolve_user_from_authorization(authorization, session)
    if user.wallet_address is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="wallet_not_bound",
        )
    return user


async def get_current_user_unbound(
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Same as `get_current_user` but does NOT require a bound wallet.

    Used by routes that exist precisely to complete onboarding (the
    wallet-bind step itself, `GET /api/wallet/me` so the FE can render
    "needs wallet" UI, etc).
    """
    return await _resolve_user_from_authorization(authorization, session)


async def _resolve_user_from_authorization(
    authorization: str | None,
    session: AsyncSession,
) -> User:
    """Shared parsing path for both `get_current_user` variants.

    RFC 7235 §2.1 / RFC 6750 §2.1 — auth scheme is case-insensitive. Some
    clients (curl with `-H 'authorization: bearer ...'`, lower-cased
    HTTP libs) would fail a strict `startswith('Bearer ')` check. Split
    on first whitespace + compare scheme casefold-equal.
    """
    if not authorization:
        raise JWTError("missing bearer token")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise JWTError("missing bearer token")
    token = parts[1].strip()
    if not token:
        raise JWTError("missing bearer token")
    claims = verify_jwt(token)
    user_id = claims["user_id"]

    # `session.get` is the idiomatic PK lookup — clearer than a hand-
    # rolled `select(User).where(User.id == ...)` and equivalent in cost.
    user = await session.get(User, user_id)
    if user is None:
        # Mint→delete race: token was valid at issue, user has since
        # vanished. Log once, return 401 (not 404) so we don't leak
        # whether the id existed.
        log.info("jwt_user_vanished", user_id=user_id)
        raise JWTError("invalid token")
    return user
