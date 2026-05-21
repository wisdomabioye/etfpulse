"""SIWE (Sign-In-With-Ethereum / EIP-4361) wallet binding helpers.

PR D.4.2 — the web-side wallet authentication primitive. Issues
single-use nonces, parses the SIWE message the wallet signed, recovers
the signer address, validates domain + chainId + nonce, and returns
the lowercased Ethereum address the JWT mint path uses.

This module imports `eth_account` *indirectly* via the `siwe` library
for signature recovery. Anti-drift rule 27 is path-scoped to
`etfpulse/adapters/sodex/*` — recovery (not signing) outside that
package is allowed. The backend STILL never holds a private key (per
CLAUDE.md "Conventions to respect"); we only verify signatures the
user's wallet already produced.

Concurrency model:
  - `_NONCE_CACHE` is an in-process `cachetools.TTLCache`, keyed by
    nonce. Single asyncio loop → no preemption between read + delete.
    Multi-worker uvicorn would split the cache; a nonce issued by
    worker A is unusable on worker B. Documented as the single-worker
    trade-off (same posture as `_ephemeral_secret` in `auth.py`).
  - Cache size cap (`_NONCE_CACHE_MAX`) bounds memory under burst load
    or a hostile-client probing the `/nonce` endpoint. LRU eviction
    on overflow: an attacker spamming `/nonce` cannot starve the legit
    pool indefinitely.

Replay defense:
  - Nonce is single-use: consumed on first successful verify. A
    captured signed message cannot be re-submitted.
  - `wallet_nonce_ttl_seconds` (default 600) bounds the window.
  - `siwe.SiweMessage.verify(signature, ...)` checks signature
    recovers to the message's claimed `address` — so an intercepted
    nonce can't be paired with a different address.
  - `domain` check defeats cross-site re-use: a SIWE message signed
    for phisher.example never verifies for `etfpulse.app`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from cachetools import TTLCache
from fastapi import HTTPException, status
from siwe import (
    DomainMismatch,
    ExpiredMessage,
    InvalidSignature,
    MalformedSession,
    NonceMismatch,
    NotYetValidMessage,
    SiweMessage,
    VerificationError,
    generate_nonce,
)

from etfpulse.config import settings

log = structlog.get_logger(__name__)


# Memory cap on the in-flight nonce pool. 1024 covers a generous burst
# of concurrent unauthenticated `/nonce` requests; LRU evicts on
# overflow so a hostile probe can't starve legitimate use.
_NONCE_CACHE_MAX = 1024


# Module-level lazy cache. The TTL is read once at first access from
# `settings.wallet_nonce_ttl_seconds`; tests patching the setting need
# to also reset the cache (see `reset_nonce_cache_for_tests`).
_NONCE_CACHE: TTLCache[str, str] | None = None


def _get_cache() -> TTLCache[str, str]:
    """Return the lazy module cache, building it on first access.

    Lazy so the TTL reads `settings` AFTER the test conftest has
    mutated it. Building at import time would freeze the TTL to
    whatever was in `settings` at module-load.
    """
    global _NONCE_CACHE
    if _NONCE_CACHE is None:
        _NONCE_CACHE = TTLCache(maxsize=_NONCE_CACHE_MAX, ttl=settings.wallet_nonce_ttl_seconds)
    return _NONCE_CACHE


def reset_nonce_cache_for_tests() -> None:
    """Drop the cache so a new TTL takes effect.

    Production never calls this; tests that twiddle
    `wallet_nonce_ttl_seconds` use it to rebuild the cache with the
    new TTL. Also handy for test isolation (a test that consumed a
    nonce won't leak state to the next).
    """
    global _NONCE_CACHE
    _NONCE_CACHE = None


class SiweVerifyError(HTTPException):
    """400 raised when SIWE parsing / verification fails.

    Detail strings are deliberately specific (`"nonce expired"` vs
    `"domain mismatch"` etc) — unlike the JWTError opacity policy,
    these failures land BEFORE any user identity is established. A
    legitimate caller with a clock-skew bug or a misconfigured
    frontend benefits from knowing exactly which check failed; an
    attacker probing the endpoint learns nothing useful because
    they'd need the signing wallet's private key to get past these
    checks anyway.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


# ---------------------------------------------------------------------------
# Nonce issuance
# ---------------------------------------------------------------------------


def issue_nonce(address: str) -> tuple[str, datetime, datetime]:
    """Generate a fresh single-use nonce bound to `address`.

    `siwe.generate_nonce()` emits a 17-char alphanumeric per EIP-4361
    §3.4 ("at least 8 alphanumeric characters"). We store
    `{nonce: address.lower()}` so verify can cross-check the recovered
    address against the address the nonce was issued for — defending
    against the cross-binding attack (attacker requests nonce for
    address A, gets user B to sign with B's wallet, submits B's
    signature with A's nonce).

    Issuing multiple nonces for the same address is permitted: each
    nonce is independently consumable. Useful when a user opens
    multiple tabs and starts the flow in each.

    Returns `(nonce, issued_at, expires_at)`. The timestamps mirror
    EIP-4361 ISO-8601 message fields the FE will embed.
    """
    if not _is_valid_eth_address(address):
        raise SiweVerifyError("invalid address format")
    nonce = generate_nonce()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.wallet_nonce_ttl_seconds)
    _get_cache()[nonce] = address.lower()
    log.info("siwe_nonce_issued", address=address.lower(), nonce_prefix=nonce[:4])
    return nonce, now, expires_at


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def consume_and_verify(*, message: str, signature: str) -> str:
    """Parse + verify a SIWE message + signature.

    Returns the lowercased Ethereum address that signed the message,
    suitable for `User.wallet_address` upsert. Raises `SiweVerifyError`
    on any validation failure.

    Verification chain (each step short-circuits to a 400 with a
    distinct detail):

      1. Parse the message string. Malformed → 400.
      2. Domain MUST equal `settings.siwe_domain`. Phishing defense.
      3. Chain ID MUST equal `settings.sodex_chain_id`. Defends
         testnet-vs-mainnet wallet binding mistakes.
      4. Nonce MUST be in our store (issued + not yet consumed/
         expired). Replay defense.
      5. Recovered signer MUST equal `nonce_store[nonce]` (the
         address we issued the nonce for). Cross-binding defense.
      6. `siwe.verify` validates the signature recovers to the
         message's `address` AND any `expirationTime`/`notBefore`
         clauses. Failure → 400.
      7. Consume the nonce (delete from store). Subsequent submits
         of the same message+signature → "nonce already used".

    The address normalization is lowercase throughout — DB CHECK
    constraint `^0x[0-9a-f]{40}$` enforces lowercase, so every
    comparison + write is lowercased to keep the contract single-
    source-of-truth.
    """
    expected_domain = settings.siwe_domain
    if not expected_domain:
        # Server misconfig — FRONTEND_URL empty. Don't operate; the
        # prod preflight already hard-errors this case, but in dev
        # a misconfigured run shouldn't silently accept any domain.
        log.error("siwe_verify_no_domain")
        raise SiweVerifyError("server: siwe domain not configured")

    expected_chain_id = settings.sodex_chain_id

    # --- 1. Parse -----------------------------------------------------------
    try:
        parsed = SiweMessage.from_message(message)
    except (ValueError, MalformedSession) as exc:
        log.info("siwe_verify_parse_failed", error=str(exc))
        raise SiweVerifyError("malformed siwe message") from exc

    # --- 2. Domain ----------------------------------------------------------
    if parsed.domain != expected_domain:
        log.info(
            "siwe_verify_domain_mismatch",
            expected=expected_domain,
            got=parsed.domain,
        )
        raise SiweVerifyError("domain mismatch")

    # --- 3. Chain ID --------------------------------------------------------
    if parsed.chain_id != expected_chain_id:
        log.info(
            "siwe_verify_chain_mismatch",
            expected=expected_chain_id,
            got=parsed.chain_id,
        )
        raise SiweVerifyError("chain_id mismatch")

    # --- 4. Nonce in store --------------------------------------------------
    cache = _get_cache()
    issued_for = cache.get(parsed.nonce)
    if issued_for is None:
        # Either never issued, already consumed, or TTL-expired. Same
        # response either way (client just re-requests a nonce).
        log.info("siwe_verify_nonce_unknown", nonce_prefix=parsed.nonce[:4])
        raise SiweVerifyError("nonce expired or unknown")

    # --- 5. Address binding -------------------------------------------------
    # `parsed.address` is EIP-55 checksum format (siwe enforces this);
    # `issued_for` is lowercased (we stored it that way). Compare on
    # lowercase to make the check case-insensitive without trusting
    # one side to have done the casing.
    claimed_address: str = parsed.address.lower()
    if claimed_address != issued_for:
        log.warning(
            "siwe_verify_address_binding_mismatch",
            issued_for=issued_for,
            claimed=claimed_address,
        )
        raise SiweVerifyError("address mismatch")

    # --- 6. Signature + clock checks (delegate to siwe lib) -----------------
    # `siwe.verify` cross-checks signature → recovered address ==
    # message.address. Also checks `expirationTime` / `notBefore` if
    # the message includes them. We pass `domain` + `nonce` again for
    # defense-in-depth; the lib does the same checks we did above.
    try:
        parsed.verify(
            signature,
            domain=expected_domain,
            nonce=parsed.nonce,
            timestamp=datetime.now(UTC),
        )
    except InvalidSignature as exc:
        log.info("siwe_verify_bad_signature", error=str(exc))
        raise SiweVerifyError("invalid signature") from exc
    except ExpiredMessage as exc:
        log.info("siwe_verify_message_expired", error=str(exc))
        raise SiweVerifyError("message expired") from exc
    except NotYetValidMessage as exc:
        log.info("siwe_verify_message_not_yet_valid", error=str(exc))
        raise SiweVerifyError("message not yet valid") from exc
    except (DomainMismatch, NonceMismatch) as exc:
        # We already checked these explicitly above; reaching here
        # would mean siwe lib + our check disagreed. Defensive belt.
        log.warning("siwe_verify_lib_cross_check_failed", error=str(exc))
        raise SiweVerifyError("siwe verification failed") from exc
    except VerificationError as exc:
        # Catch-all for the rest. Distinguishable in logs by
        # `error_type` but collapsed in client response.
        log.info(
            "siwe_verify_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise SiweVerifyError("siwe verification failed") from exc
    except Exception as exc:
        # eth_account / eth_keys raise their own non-siwe exception
        # classes (`BadSignature`, `ValueError`) when an r/s pair is so
        # malformed that ECDSA recovery itself fails — these don't
        # inherit `VerificationError` so the catches above miss them.
        # At this point in the flow steps 1-5 already validated message
        # structure; any failure surfaced by `parsed.verify()` is by
        # definition a signature / crypto problem and maps cleanly to
        # the same client-visible 400. Without this catch the route
        # would 500 on certain tampered signatures.
        #
        # Log level is WARNING (not info) — this branch also catches
        # programmer-error categories like `AttributeError` or
        # `KeyError` that would otherwise have surfaced as 500.
        # Collapsing them to 400 keeps the client experience uniform
        # but operators need the louder log to notice a real bug
        # masquerading as a routine "bad signature" event.
        # `error_type` carries the distinct exception class for
        # post-hoc triage (BadSignature vs ValueError vs anything
        # genuinely unexpected).
        log.warning(
            "siwe_verify_crypto_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise SiweVerifyError("invalid signature") from exc

    # --- 7. Consume the nonce ----------------------------------------------
    # Single-use. A replay of the same message+signature now finds an
    # empty slot and rejects at step 4.
    cache.pop(parsed.nonce, None)
    log.info("siwe_verify_ok", address=claimed_address)
    return claimed_address


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_valid_eth_address(address: str) -> bool:
    """Surface-level shape check: 0x + 40 hex chars (any case).

    Lighter than EIP-55 checksum validation (which would refuse
    lowercase-only). The route accepts the user's wallet's chosen
    casing and lowercases for storage; the storage constraint is
    `^0x[0-9a-f]{40}$`.
    """
    if not isinstance(address, str) or len(address) != 42:
        return False
    if not address.startswith("0x"):
        return False
    try:
        int(address[2:], 16)
    except ValueError:
        return False
    return True
