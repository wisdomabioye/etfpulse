"""Execution orchestrator — prepare/submit for new orders and cancels.

This module composes D.1 (typed-data builders) + D.2 (HTTP clients) +
D.3 leaves (risk, nonce, symbols, positions, paper). Four public
functions form the V1 contract:

  - `prepare_new` — Lock user (FOR UPDATE), risk-check, resolve symbol,
    generate nonce + clOrdID, build EIP-712 typed-data, INSERT Order
    in PENDING. Returns typed_data for the wallet to sign.

  - `submit_new` — Validate signature, transition PENDING → SUBMITTED,
    POST to gateway (or simulate paper fill), record ACKED/REJECTED.

  - `prepare_cancel` — Status-branched. PENDING → local CANCELLED.
    SUBMITTED → reject ("in flight, wait for ACK"). ACKED/PARTIALLY_FILLED
    → lock + nonce + build cancel typed-data + persist on Order row.
    Terminal → idempotent no-op.

  - `submit_cancel` — Validate signature, persist + DELETE to gateway
    (or paper-cancel locally). Record CANCELLED or `cancel_error_message`.

State-machine reference (every transition for both venues):

```
PENDING ─sign─→ SUBMITTED ─ack─→ ACKED ─reconcile fill─→ PARTIALLY_FILLED ─→ FILLED
   │                │                │              │
   │                │                │              └─cancel─→ CANCELLED (filled_size preserved)
   │                │                └─cancel─→ CANCELLED
   │                │                └─nonce expiry─→ EXPIRED
   │                └─reject─→ REJECTED
   │                └─reconcile missing + nonce expired─→ EXPIRED
   └─local cancel─→ CANCELLED (no venue contact; never signed)
   └─nonce expiry─→ EXPIRED (signed but never submitted)
```

D14: no commits in this module. The route owns the transaction
boundary. Crash-recovery story: if we crash mid-HTTP, the tx rolls
back, status returns to its pre-call state. Boot-time orphan scan
(`boot_recovery.py`) + reconcile loop resolve any drift.

Anti-drift rules invoked here:
  - 28 — no signing primitives (we accept signatures from caller; never
    generate them).
  - 30 — `prepare_new` opens the per-user FOR UPDATE lock via
    `check_order`; nonce + INSERT happen inside that lock.
  - 31 — Order.eip712_payload is the byte-exact TEXT bytes;
    `bytes_helpers.extract_params_bytes` is the only reader.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sodex import SodexAuthError, SodexEnvelopeError
from etfpulse.adapters.sodex.builders import (
    TypedDataBundle,
    build_perps_cancel_order,
    build_perps_new_order,
    build_spot_cancel_order,
    build_spot_new_order,
)
from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.schemas import (
    OrderModifier,
    PerpsCancelOrderRequest,
    PerpsNewOrderRequest,
    SpotBatchCancelOrderRequest,
    SpotBatchNewOrderRequest,
)
from etfpulse.adapters.sodex.schemas import (
    OrderSide as SodexOrderSide,
)
from etfpulse.adapters.sodex.schemas import (
    OrderType as SodexOrderType,
)
from etfpulse.adapters.sodex.schemas import (
    StopType as SodexStopType,
)
from etfpulse.adapters.sodex.schemas import (
    TimeInForce as SodexTimeInForce,
)
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.config import settings
from etfpulse.models.order import (
    TERMINAL_ORDER_STATUSES,
    Order,
    OrderStatus,
    Venue,
)
from etfpulse.models.order import (
    OrderSide as DbOrderSide,
)
from etfpulse.models.order import (
    OrderType as DbOrderType,
)
from etfpulse.models.order import (
    StopType as DbStopType,
)
from etfpulse.models.order import (
    TimeInForce as DbTimeInForce,
)
from etfpulse.pipeline.execution import paper
from etfpulse.pipeline.execution.bytes_helpers import extract_params_bytes
from etfpulse.pipeline.execution.nonce import next_nonce_for_user
from etfpulse.pipeline.execution.positions_perps import perps_apply_fill
from etfpulse.pipeline.execution.positions_spot import spot_apply_fill
from etfpulse.pipeline.execution.risk import RiskDecision, RiskRequest, check_order
from etfpulse.pipeline.execution.symbols import resolve_symbol_id

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrepareResult:
    """Result of `prepare_new` / `prepare_cancel`.

    On `allow=True` for new orders + non-local-only cancels: the route
    returns `typed_data` to the frontend for wallet signing; on
    completion, the route calls `submit_*` with the resulting signature.

    `local_only=True` is a cancel-only state: the order was PENDING
    (never signed) and we transitioned to CANCELLED entirely in the DB.
    No wallet signature is needed.

    `replayed=True` on cancel-of-terminal: idempotent no-op (already
    cancelled / filled / expired). Caller can return current state to
    the user.
    """

    allow: bool
    order_id: int | None = None
    typed_data: dict | None = None
    client_order_id: str | None = None
    nonce: int | None = None
    local_only: bool = False
    replayed: bool = False
    reason: str | None = None
    detail: str | None = None
    breaker_trigger: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Result of `submit_new` / `submit_cancel`.

    `status` is the Order's status AFTER the submit. `replayed=True`
    means we short-circuited on idempotency (status was already past
    the entry-state at call time).
    """

    status: str
    order_id: int
    exchange_order_id: str | None = None
    error_message: str | None = None
    replayed: bool = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_VENUE_TO_SHORT: dict[str, str] = {
    Venue.SODEX_SPOT.value: "s",
    Venue.SODEX_PERPS.value: "p",
}

_CLORDID_MAX_INSERT_ATTEMPTS = 3

# Nonce window per api.md: `(T-2d, T+1d)` ms. We pin our local
# expiry at +1 day so the reaper terminalises an order whose nonce
# would no longer verify if re-submitted (the protocol still allows
# `T+1d` exactly, but after that any retry fails).
_NONCE_WINDOW_HOURS = 24

# Map SoDEX IntEnum values → DB StrEnum values. The RiskRequest carries
# IntEnums (what the gateway sees); persisted Order fields use StrEnums
# (what the DB stores in VARCHAR columns). Same name, different type —
# this map is the explicit boundary.
_SODEX_TO_DB_SIDE: dict[int, str] = {
    SodexOrderSide.BUY.value: DbOrderSide.BUY.value,
    SodexOrderSide.SELL.value: DbOrderSide.SELL.value,
}
_SODEX_TO_DB_TYPE: dict[int, str] = {
    SodexOrderType.LIMIT.value: DbOrderType.LIMIT.value,
    SodexOrderType.MARKET.value: DbOrderType.MARKET.value,
}
_SODEX_TO_DB_TIF: dict[int, str] = {
    SodexTimeInForce.GTC.value: DbTimeInForce.GTC.value,
    SodexTimeInForce.IOC.value: DbTimeInForce.IOC.value,
    SodexTimeInForce.FOK.value: DbTimeInForce.FOK.value,
    SodexTimeInForce.GTX.value: DbTimeInForce.GTX.value,
}

# PR P1-fix.CRIT-1 — DB StopType string → SoDEX wire IntEnum. The
# RiskRequest carries the DB string (`"stop_loss"`/`"take_profit"`) for
# the perps stop legs; the signed payload needs the integer. The
# `models.order.StopType` docstring documents this boundary explicitly.
_DB_TO_SODEX_STOP_TYPE: dict[str, int] = {
    DbStopType.STOP_LOSS.value: SodexStopType.STOP_LOSS.value,
    DbStopType.TAKE_PROFIT.value: SodexStopType.TAKE_PROFIT.value,
}

# Signature regex. `0x01` (SoDEX type byte) + 65-byte ECDSA hex.
_EIP712_SIGNATURE_RE = re.compile(r"^0x01[0-9a-f]{130}$")


# ---------------------------------------------------------------------------
# prepare_new
# ---------------------------------------------------------------------------


async def prepare_new(
    session: AsyncSession,
    *,
    user_id: int,
    request: RiskRequest,
    signal_id: int | None = None,
) -> PrepareResult:
    """Lock the user, run risk checks, build EIP-712 typed-data, INSERT
    the Order row in PENDING.

    `signal_id` is optional — when a signal-driven order is being placed
    (a `Signal` row drove the suggested entry/levels), the FK back to
    that signal carries through onto Order.signal_id for analytics and
    audit. None for ad-hoc orders.

    The FOR UPDATE lock taken by `check_order` is held through this
    entire function and released only when the caller commits (or
    rolls back) the transaction.
    """
    decision: RiskDecision
    decision, user = await check_order(session, user_id=user_id, request=request)
    if not decision.allow:
        return PrepareResult(
            allow=False,
            reason=decision.reason,
            detail=decision.detail,
            breaker_trigger=decision.breaker_trigger,
        )

    # Resolve symbol_id (sodex_symbols cache lookup). Risk-side validation
    # ensures the venue is supported; if the cache lacks a row, raise so
    # the route returns 503 with an operator-actionable message.
    symbol_id = await resolve_symbol_id(session, request.venue, request.asset)

    # Generate per-user monotonic nonce under the lock we hold.
    nonce = await next_nonce_for_user(session, user_id=user_id)
    nonce_expires_at = datetime.now(UTC) + timedelta(hours=_NONCE_WINDOW_HOURS)

    # INSERT with clOrdID-collision retry. `client_order_id` is UNIQUE;
    # token_urlsafe(8) yields 11 random URL-safe chars per ID — same-
    # millisecond collision probability is ~1/2^64. We still defend
    # against it with up to 3 SAVEPOINTed retries; the outer FOR UPDATE
    # lock survives each savepoint rollback unaffected.
    order, bundle = await _insert_order_with_clordid_retry(
        session=session,
        user=user,
        request=request,
        signal_id=signal_id,
        symbol_id=symbol_id,
        nonce=nonce,
        nonce_expires_at=nonce_expires_at,
    )

    log.info(
        "execution_prepared_new",
        user_id=user_id,
        order_id=order.id,
        client_order_id=order.client_order_id,
        venue=request.venue,
        asset=request.asset,
        side=request.side,
        nonce=nonce,
        paper=user.paper_trade,
    )

    return PrepareResult(
        allow=True,
        order_id=order.id,
        typed_data=bundle.typed_data,
        client_order_id=order.client_order_id,
        nonce=nonce,
    )


# ---------------------------------------------------------------------------
# submit_new
# ---------------------------------------------------------------------------


async def submit_new(
    session: AsyncSession,
    *,
    order_id: int,
    signature: str,
    spot_client: SodexSpotClient,
    perps_client: SodexPerpsClient,
    api_key_name_resolver=None,
) -> SubmitResult:
    """Validate signature, transition PENDING → SUBMITTED, POST to gateway
    (or paper-fill), record ACKED/REJECTED.

    `api_key_name_resolver(user, venue) -> str` defaults to reading the
    per-venue api_key column on User; tests can inject a mock to bypass
    DB-side state. Production uses the default.

    Idempotency: if the order is already past PENDING (e.g. frontend
    retried), we return the current state with `replayed=True` rather
    than re-submitting. This closes the "user double-clicks submit"
    duplication path.

    Crash-recovery: this function does NOT commit. The route owns the
    transaction. If the route crashes mid-HTTP, the tx rolls back, the
    Order returns to PENDING, and the user can re-sign (with the SAME
    stored nonce). The gateway rejects nonce replays per its tracking,
    so duplicate execution is structurally impossible — see boot
    recovery + reconcile for the drift-resolution path.
    """
    order = await _load_order_or_raise(session, order_id)

    # Idempotency guard — any state past PENDING is a replay.
    if order.status != OrderStatus.PENDING.value:
        log.info(
            "execution_submit_new_replay",
            order_id=order_id,
            current_status=order.status,
        )
        return SubmitResult(
            status=order.status,
            order_id=order_id,
            exchange_order_id=order.exchange_order_id,
            error_message=order.error_message,
            replayed=True,
        )

    # Validate signature shape + nonce window.
    sig_lower = signature.lower()
    if not _EIP712_SIGNATURE_RE.fullmatch(sig_lower):
        return await _reject_submission(
            session=session,
            order=order,
            error="invalid_signature_format",
            log_event="execution_submit_new_invalid_signature",
        )

    now = datetime.now(UTC)
    # CHECK constraint requires both columns set or both NULL — both
    # were set at prepare time, so nonce_expires_at is guaranteed non-NULL
    # here. Defensive belt nonetheless:
    if order.nonce_expires_at is None or order.nonce_expires_at < now:
        return await _reject_submission(
            session=session,
            order=order,
            error="nonce_window_expired",
            log_event="execution_submit_new_nonce_expired",
        )

    # Persist signature + transition. The hash + signature link is
    # validated by the DB CHECK (signature without payload rejected).
    # Compute the payload hash from the stored payload bytes — same
    # primitive D.1 uses on the build side.
    if not order.eip712_payload:
        return await _reject_submission(
            session=session,
            order=order,
            error="missing_payload_at_submit",
            log_event="execution_submit_new_no_payload",
        )
    # PR D.3.1 — `eip712_payload_hash` was already set at INSERT time
    # by `_insert_order_with_clordid_retry` using `bundle.payload_hash`
    # (D.1's authoritative hash). Re-computing here was wasted work AND
    # a potential divergence vector if D.1's `payload.py` hashing logic
    # ever drifts.
    #
    # PR D.3.2 — runtime check (was `assert`). Python's `-O` flag strips
    # asserts; we need a real branch so the invariant holds even when
    # uvicorn is run optimized.
    if order.eip712_payload_hash is None:
        return await _reject_submission(
            session=session,
            order=order,
            error="missing_payload_hash_at_submit",
            log_event="execution_submit_new_no_payload_hash",
        )
    order.eip712_signature = sig_lower
    order.status = OrderStatus.SUBMITTED.value
    await session.flush()

    # Paper branch — short-circuit before any HTTP. The paper fill is
    # treated as instant + full; transition ACKED → FILLED in one step
    # and open/close the corresponding Position.
    if order.paper_trade:
        # PR P1-fix.PAPER-1 — a conditional (stop) order MUST NOT
        # instant-fill in paper mode: that would close the position the
        # moment the user attaches a stop, instead of waiting for the
        # trigger. Rest it ACKED ("trigger pending"), mirroring the real
        # path, until the Phase-2 paper-watcher evaluates the trigger
        # against live prices. Until that watcher ships, a paper stop
        # rests but never fires — that's the honest V1 state (no
        # protection), consistent with how a real stop would rest.
        if order.is_conditional:
            return await _ack_paper_conditional(session, order=order)
        return await _simulate_paper_fill(session, order=order)

    # Real branch — POST to the gateway.
    return await _dispatch_real_submission(
        session,
        order=order,
        spot_client=spot_client,
        perps_client=perps_client,
        api_key_name_resolver=api_key_name_resolver,
    )


# ---------------------------------------------------------------------------
# prepare_cancel
# ---------------------------------------------------------------------------


async def prepare_cancel(
    session: AsyncSession,
    *,
    user_id: int,
    order_id: int,
) -> PrepareResult:
    """Status-branched cancel preparation.

    Branches (see state machine in module docstring):
      - PENDING → local CANCELLED, no venue contact, return
        `local_only=True`. Caller commits.
      - SUBMITTED → DENY with `reason="cancel_blocked_in_flight"`.
        UI surfaces "wait for ACK." We don't queue cancel intents in
        V1 — the operational benefit doesn't justify the race-window
        complexity.
      - ACKED / PARTIALLY_FILLED → build cancel typed-data, persist on
        the Order row, return typed_data for wallet signing.
      - Terminal (CANCELLED / FILLED / REJECTED / EXPIRED) →
        `replayed=True` idempotent no-op.
    """
    order = await _load_order_or_raise(session, order_id)

    if order.user_id != user_id:
        # The route is responsible for auth; this is a defensive belt.
        return PrepareResult(
            allow=False,
            reason="unauthorized",
            detail=(
                f"Order {order_id} belongs to a different user. Auth check "
                "should have already failed upstream."
            ),
        )

    # Terminal → idempotent no-op.
    if order.status in {s.value for s in TERMINAL_ORDER_STATUSES}:
        log.info(
            "execution_prepare_cancel_terminal_noop",
            order_id=order_id,
            current_status=order.status,
        )
        return PrepareResult(
            allow=True,
            order_id=order_id,
            local_only=True,
            replayed=True,
        )

    # PENDING — local-only cancel. No venue contact; just flip the DB row.
    if order.status == OrderStatus.PENDING.value:
        order.status = OrderStatus.CANCELLED.value
        await session.flush()
        log.info(
            "execution_prepare_cancel_local_only",
            order_id=order_id,
            prior_status="pending",
        )
        return PrepareResult(
            allow=True,
            order_id=order_id,
            local_only=True,
        )

    # SUBMITTED — race window. Reject; UI surfaces "wait for ACK."
    if order.status == OrderStatus.SUBMITTED.value:
        return PrepareResult(
            allow=False,
            reason="cancel_blocked_in_flight",
            detail=(
                "Order is SUBMITTED but not yet ACKed. Wait for the "
                "gateway response before cancelling."
            ),
        )

    # ACKED / PARTIALLY_FILLED — build cancel typed-data. We need a fresh
    # nonce under the per-user FOR UPDATE lock (anti-drift rule 30 — same
    # concurrency primitive as new orders).
    from etfpulse.models.user import User

    locked = await session.execute(select(User).where(User.id == user_id).with_for_update())
    user = locked.scalar_one_or_none()
    if user is None:
        # Programmer-error case — auth should have caught this.
        return PrepareResult(allow=False, reason="user_not_found")

    cancel_nonce = await next_nonce_for_user(session, user_id=user_id)

    # Build the cancel request shape. Spot vs perps schemas differ.
    bundle = _build_cancel_order_bundle(
        order=order,
        account_id=user.sodex_account_id,
        cancel_nonce=cancel_nonce,
    )

    # Persist cancel payload + payload-hash + nonce on the existing
    # Order row. CHECK invariants: payload + nonce both set or both
    # NULL; signature CHECKs are NULL-safe so we can leave signature
    # for submit_cancel.
    #
    # PR D.3.2 — Reset the cancel-side signature + submitted_at +
    # error_message when a fresh cancel typed-data is generated.
    # This supports the gateway-failure recovery path: after a
    # previous submit_cancel returned a per-order rejection (e.g.,
    # transient envelope error) the next attempt MUST re-sign with
    # the freshly-generated cancel_nonce. Without clearing the prior
    # signature, submit_cancel's in-flight idempotency guard would
    # short-circuit on the stale signature.
    order.cancel_nonce = cancel_nonce
    order.cancel_eip712_payload = bundle.payload_json
    order.cancel_eip712_payload_hash = bundle.payload_hash
    order.cancel_eip712_signature = None
    order.cancel_submitted_at = None
    order.cancel_error_message = None
    await session.flush()

    log.info(
        "execution_prepared_cancel",
        order_id=order_id,
        current_status=order.status,
        cancel_nonce=cancel_nonce,
    )

    return PrepareResult(
        allow=True,
        order_id=order_id,
        typed_data=bundle.typed_data,
        client_order_id=order.client_order_id,
        nonce=cancel_nonce,
    )


# ---------------------------------------------------------------------------
# submit_cancel
# ---------------------------------------------------------------------------


async def submit_cancel(
    session: AsyncSession,
    *,
    order_id: int,
    signature: str,
    spot_client: SodexSpotClient,
    perps_client: SodexPerpsClient,
    api_key_name_resolver=None,
) -> SubmitResult:
    """Validate cancel signature, DELETE on gateway, record outcome.

    Idempotency: if the order is already CANCELLED (or other terminal),
    return current state with `replayed=True`. The frontend may retry
    submit_cancel on network blips — this guard ensures we don't
    re-attempt cancels that already landed.
    """
    order = await _load_order_or_raise(session, order_id)

    # Idempotency — terminal states (incl. already-CANCELLED) replay.
    if order.status in {s.value for s in TERMINAL_ORDER_STATUSES}:
        return SubmitResult(
            status=order.status,
            order_id=order_id,
            exchange_order_id=order.exchange_order_id,
            error_message=order.cancel_error_message or order.error_message,
            replayed=True,
        )

    # PR D.3.1 — in-flight cancel idempotency guard. If the cancel
    # signature is already persisted AND we haven't reached a terminal
    # state yet, the first submit_cancel either landed (status will
    # update on its result) or is still in flight. A second call
    # would re-use the SAME cancel_nonce with a fresh signature → the
    # gateway treats the second POST as a duplicate-signed payload
    # and rejects on replay protection. Return replayed=True so the
    # caller (typically a frontend retry on network blip) reads the
    # current state instead of re-submitting.
    #
    # PR D.3.2 — recovery path for gateway-side cancel failure. If a
    # previous submit_cancel attempt FAILED at the gateway (i.e.,
    # `cancel_error_message` was populated but status remained
    # non-terminal — e.g., transient envelope error or OrderNotFound
    # race), retrying with the SAME signature against the SAME nonce
    # would just be rejected again on replay protection. The retry
    # path is to call `prepare_cancel` again — it will overwrite the
    # cancel-side payload + nonce with a fresh pair (cancel_nonce
    # bumps, signature gets cleared), at which point this guard no
    # longer fires and submit_cancel proceeds with the new bytes.
    # Document this contract here so D.4 UI / admin tooling knows
    # the retry pattern.
    if order.cancel_eip712_signature is not None:
        return SubmitResult(
            status=order.status,
            order_id=order_id,
            exchange_order_id=order.exchange_order_id,
            error_message=order.cancel_error_message,
            replayed=True,
        )

    if not order.cancel_eip712_payload or order.cancel_nonce is None:
        return SubmitResult(
            status=order.status,
            order_id=order_id,
            error_message="cancel_not_prepared",
        )

    sig_lower = signature.lower()
    if not _EIP712_SIGNATURE_RE.fullmatch(sig_lower):
        order.cancel_error_message = "invalid_signature_format"
        await session.flush()
        return SubmitResult(
            status=order.status,
            order_id=order_id,
            error_message=order.cancel_error_message,
        )

    order.cancel_eip712_signature = sig_lower
    order.cancel_submitted_at = datetime.now(UTC)
    await session.flush()

    # Paper branch — flip to CANCELLED locally. Position from any
    # partial fill stays open (matches real-trade semantics).
    if order.paper_trade:
        order.status = OrderStatus.CANCELLED.value
        await session.flush()
        log.info(
            "execution_submit_cancel_paper",
            order_id=order_id,
        )
        return SubmitResult(status=order.status, order_id=order_id)

    # Real branch — DELETE to the gateway.
    return await _dispatch_real_cancel(
        session,
        order=order,
        spot_client=spot_client,
        perps_client=perps_client,
        api_key_name_resolver=api_key_name_resolver,
    )


# ---------------------------------------------------------------------------
# Internals — bundle building
# ---------------------------------------------------------------------------


def _make_client_order_id(venue: str) -> str:
    """Generate a ULID-flavoured client_order_id matching the ClOrdID
    regex `^[0-9a-zA-Z_-]{1,36}$`. Format: `ep-{s|p}-{token}`."""
    short = _VENUE_TO_SHORT.get(venue)
    if short is None:
        raise ValueError(f"_make_client_order_id: unknown venue {venue!r}")
    # token_urlsafe(8) yields 11 chars from [A-Za-z0-9_-]. Total
    # length: 4 ('ep-X-') + 11 = 15 chars — well under 36.
    return f"ep-{short}-{secrets.token_urlsafe(8)}"


def _build_new_order_bundle(
    *,
    request: RiskRequest,
    account_id: int | None,
    symbol_id: int,
    nonce: int,
    client_order_id: str,
) -> TypedDataBundle:
    """Build the typed-data bundle WITHOUT persisting. Returns the
    bundle whose payload_json is the byte-exact value the wallet will
    sign. Caller persists.

    `account_id` is the cached `User.sodex_account_id`. Risk-side
    validation ensures it's non-NULL before we reach here, but Pydantic
    requires `int` not `int | None` — defensive cast + assertion.

    `client_order_id` is passed in (not generated here) so the caller
    owns regeneration on UNIQUE collision — the bundle signs against
    the same value that gets INSERTed.
    """
    if account_id is None:
        raise ValueError("_build_new_order_bundle: account_id must be cached on the user")

    # D.1 schemas use camelCase aliases (`accountID`, `clOrdID`, …).
    # `populate_by_name=True` accepts both at runtime, but mypy only sees
    # the aliases as the canonical Pydantic field names — build via
    # `model_validate({...})` so the dict-key form satisfies the type
    # checker AND matches the wire shape exactly.
    if request.venue == Venue.SODEX_SPOT.value:
        spot_req = SpotBatchNewOrderRequest.model_validate(
            {
                "accountID": account_id,
                "orders": [
                    {
                        "symbolID": symbol_id,
                        "clOrdID": client_order_id,
                        "side": request.side,
                        "type": request.order_type,
                        "timeInForce": request.time_in_force,
                        "price": _decimal_to_str(request.requested_price),
                        "quantity": _decimal_to_str(request.requested_size),
                        "funds": None,
                    }
                ],
            }
        )
        return build_spot_new_order(
            request=spot_req,
            chain_id=settings.sodex_chain_id,
            nonce=nonce,
        )

    if request.venue == Venue.SODEX_PERPS.value:
        # Perps requires position_side + reduce_only on every order
        # (api.md "required-with-zero-value invariant"). Risk gate
        # enforces position_side IS NOT None for perps; defensive.
        if request.position_side is None:
            raise ValueError(
                "_build_new_order_bundle: perps order requires position_side "
                "(risk gate should have rejected)"
            )
        # PR P1-fix.CRIT-1 — thread the protective-leg fields into the
        # SIGNED payload. Pre-fix these were hardcoded `stopPrice=None`,
        # `stopType=None`, `reduceOnly=False`, so a "stop-loss" was
        # signed + submitted as a plain market order (fired instantly at
        # signing, never at the trigger) and a "close" was NOT
        # reduce-only (could oversell into opposite exposure). The
        # PerpsOrderItem schema has carried these fields since D.1
        # ("for TP/SL legs"); the orchestrator just never populated
        # them. `stopType` maps DB string → SoDEX wire int.
        sodex_stop_type = (
            _DB_TO_SODEX_STOP_TYPE[request.stop_type] if request.stop_type is not None else None
        )
        perps_req = PerpsNewOrderRequest.model_validate(
            {
                "accountID": account_id,
                "symbolID": symbol_id,
                "orders": [
                    {
                        "clOrdID": client_order_id,
                        "modifier": OrderModifier.NORMAL.value,
                        "side": request.side,
                        "type": request.order_type,
                        "timeInForce": request.time_in_force,
                        "price": _decimal_to_str(request.requested_price),
                        "quantity": _decimal_to_str(request.requested_size),
                        "funds": None,
                        "stopPrice": _decimal_to_str(request.stop_price),
                        "stopType": sodex_stop_type,
                        "triggerType": request.trigger_type,
                        "reduceOnly": request.reduce_only,
                        "positionSide": request.position_side,
                    }
                ],
            }
        )
        return build_perps_new_order(
            request=perps_req,
            chain_id=settings.sodex_chain_id,
            nonce=nonce,
        )

    raise ValueError(f"_build_new_order_bundle: unsupported venue {request.venue!r}")


def _build_cancel_order_bundle(
    *,
    order: Order,
    account_id: int | None,
    cancel_nonce: int,
) -> TypedDataBundle:
    if account_id is None:
        raise ValueError("_build_cancel_order_bundle: account_id must be cached on the user")
    if order.symbol_id is None:
        raise ValueError(f"_build_cancel_order_bundle: order id={order.id} has no symbol_id")

    # Identify the order on the venue via exchange_order_id (preferred)
    # OR original client_order_id (fallback when ACK landed but
    # exchange_order_id failed to persist — should be rare).
    venue_order_id: int | None = None
    if order.exchange_order_id is not None:
        try:
            venue_order_id = int(order.exchange_order_id)
        except ValueError:
            venue_order_id = None
    orig_client_order_id = order.client_order_id

    if order.venue == Venue.SODEX_SPOT.value:
        spot_req = SpotBatchCancelOrderRequest.model_validate(
            {
                "accountID": account_id,
                "cancels": [
                    {
                        "symbolID": order.symbol_id,
                        "clOrdID": orig_client_order_id,
                        "orderID": venue_order_id,
                        "origClOrdID": orig_client_order_id,
                    }
                ],
            }
        )
        return build_spot_cancel_order(
            request=spot_req,
            chain_id=settings.sodex_chain_id,
            nonce=cancel_nonce,
        )

    if order.venue == Venue.SODEX_PERPS.value:
        perps_req = PerpsCancelOrderRequest.model_validate(
            {
                "accountID": account_id,
                "cancels": [
                    {
                        "symbolID": order.symbol_id,
                        "orderID": venue_order_id,
                        "clOrdID": orig_client_order_id,
                    }
                ],
            }
        )
        return build_perps_cancel_order(
            request=perps_req,
            chain_id=settings.sodex_chain_id,
            nonce=cancel_nonce,
        )

    raise ValueError(f"_build_cancel_order_bundle: unsupported venue {order.venue!r}")


def _decimal_to_str(value: Decimal | None) -> str | None:
    """Decimal → DecimalString-formatted string. Pydantic's
    DecimalString regex rejects scientific notation; `str(Decimal)`
    can emit `1E+8` for trailing zeros if the Decimal was constructed
    from e.g. `Decimal('1e8')`. Coerce through f-string formatting to
    guarantee plain digit-and-dot output."""
    if value is None:
        return None
    # `:f` format avoids scientific notation; trailing zeros are kept
    # if the Decimal carries them, which matches DecimalString semantics
    # ("the gateway accepts arbitrary trailing zeros").
    return f"{value:f}"


# ---------------------------------------------------------------------------
# Internals — INSERT + retry
# ---------------------------------------------------------------------------


async def _insert_order_with_clordid_retry(
    *,
    session: AsyncSession,
    user,
    request: RiskRequest,
    signal_id: int | None,
    symbol_id: int,
    nonce: int,
    nonce_expires_at: datetime,
) -> tuple[Order, TypedDataBundle]:
    """INSERT the Order row using a SAVEPOINT per attempt so a clOrdID
    UNIQUE collision can be retried without rolling back the outer
    transaction (and losing the FOR UPDATE lock).

    Returns `(order, bundle)` — bundle is needed by the caller to
    derive `typed_data` for the wallet. Bundle's payload_json matches
    Order.eip712_payload exactly.

    Raises the last IntegrityError if all attempts collide (or the
    error is not a clOrdID violation — CHECK violations bubble up
    immediately).
    """
    last_exception: IntegrityError | None = None

    for attempt in range(_CLORDID_MAX_INSERT_ATTEMPTS):
        client_order_id = _make_client_order_id(request.venue)
        bundle = _build_new_order_bundle(
            request=request,
            account_id=user.sodex_account_id,
            symbol_id=symbol_id,
            nonce=nonce,
            client_order_id=client_order_id,
        )

        # Inside a SAVEPOINT. If flush raises, only the SAVEPOINT rolls
        # back; the outer tx (and our FOR UPDATE on the user row)
        # survives untouched.
        order = Order(
            user_id=user.id,
            signal_id=signal_id,
            venue=request.venue,
            asset=request.asset,
            side=_SODEX_TO_DB_SIDE[request.side],
            order_type=_SODEX_TO_DB_TYPE[request.order_type],
            time_in_force=_SODEX_TO_DB_TIF[request.time_in_force],
            requested_size=request.requested_size,
            requested_price=request.requested_price,
            status=OrderStatus.PENDING.value,
            client_order_id=client_order_id,
            paper_trade=user.paper_trade,
            account_id=user.sodex_account_id,
            symbol_id=symbol_id,
            nonce=nonce,
            nonce_expires_at=nonce_expires_at,
            eip712_payload=bundle.payload_json,
            eip712_payload_hash=bundle.payload_hash,
            is_conditional=request.is_conditional,
            stop_price=request.stop_price,
            stop_type=request.stop_type,
            reduce_only=request.reduce_only,
            parent_order_id=request.parent_order_id,
        )

        try:
            async with session.begin_nested():
                session.add(order)
                # SAVEPOINT commit triggers flush; IntegrityError raises here.
            return order, bundle
        except IntegrityError as exc:
            last_exception = exc
            # Only retry on clOrdID UNIQUE; re-raise any other integrity
            # error (CHECK violations etc) immediately.
            if "client_order_id" not in str(exc).lower():
                raise
            log.warning(
                "execution_clordid_collision_retry",
                attempt=attempt + 1,
                client_order_id=client_order_id,
            )
            # Loop continues with a fresh clOrdID + rebuilt bundle.

    # Exhausted attempts — surface the last collision.
    assert last_exception is not None
    raise last_exception


# ---------------------------------------------------------------------------
# Internals — submit dispatch
# ---------------------------------------------------------------------------


def _default_api_key_resolver(user, venue: str) -> str:
    if venue == Venue.SODEX_SPOT.value:
        name = user.sodex_spot_api_key_name
    elif venue == Venue.SODEX_PERPS.value:
        name = user.sodex_perps_api_key_name
    else:
        raise ValueError(f"_default_api_key_resolver: unknown venue {venue!r}")
    if not name:
        raise ValueError(f"_default_api_key_resolver: user {user.id} has no api_key for {venue}")
    # mypy widens the SQLAlchemy mapped attribute to `Any`; the narrowing
    # above guarantees it's a non-empty str.
    return str(name)


async def _ack_paper_conditional(session: AsyncSession, *, order: Order) -> SubmitResult:
    """PR P1-fix.PAPER-1 — paper-trade conditional (stop) order: rest at
    ACKED instead of instant-filling. No Position change (the trigger
    hasn't fired). `ACKED + is_conditional=True` is the "trigger pending"
    state the model documents; the Phase-2 paper-watcher will pick it up
    and fill it when the stop price is breached."""
    order.exchange_order_id = f"PAPER-{order.id}"
    order.status = OrderStatus.ACKED.value
    await session.flush()
    log.info(
        "execution_paper_conditional_acked",
        order_id=order.id,
        stop_type=order.stop_type,
        stop_price=str(order.stop_price) if order.stop_price is not None else None,
    )
    return SubmitResult(
        status=order.status,
        order_id=order.id,
        exchange_order_id=order.exchange_order_id,
    )


async def _simulate_paper_fill(session: AsyncSession, *, order: Order) -> SubmitResult:
    fill_price, fill_size = paper.simulate_fill(order)
    order.exchange_order_id = f"PAPER-{order.id}"

    # Apply to the Position table FIRST so we can detect the
    # PR P1-fix.RO-FILL-1 no-op: a reduce_only fill with nothing to reduce
    # returns None (`perps_apply_fill` refuses to open/extend). `apply_fill`
    # takes fill_price/fill_size as explicit kwargs and does NOT read
    # `order.filled_*`, so applying before stamping the fill fields is safe.
    if order.venue == Venue.SODEX_SPOT.value:
        # Spot never carries reduce_only (risk gate is perps-only), so this
        # always opens/extends/reduces and returns a Position.
        position = await spot_apply_fill(
            session, order=order, fill_price=fill_price, fill_size=fill_size
        )
    elif order.venue == Venue.SODEX_PERPS.value:
        position = await perps_apply_fill(
            session, order=order, fill_price=fill_price, fill_size=fill_size
        )
    else:
        raise ValueError(f"_simulate_paper_fill: unknown venue {order.venue!r}")

    if position is None and order.reduce_only:
        # Reduce_only with no reducible position — mirror the real gateway
        # and REJECT rather than record a phantom FILLED with no position
        # effect. (Only reachable on perps; spot can't be reduce_only.)
        order.status = OrderStatus.REJECTED.value
        order.error_message = "paper: reduce_only order had no position to reduce"
        await session.flush()
        log.info("execution_paper_reduce_only_noop_rejected", order_id=order.id)
        return SubmitResult(
            status=order.status,
            order_id=order.id,
            exchange_order_id=order.exchange_order_id,
            error_message=order.error_message,
        )

    order.filled_price = fill_price
    order.filled_size = fill_size
    order.filled_value = fill_price * fill_size
    order.status = OrderStatus.FILLED.value
    await session.flush()

    log.info(
        "execution_paper_fill",
        order_id=order.id,
        fill_price=str(fill_price),
        fill_size=str(fill_size),
    )
    return SubmitResult(
        status=order.status,
        order_id=order.id,
        exchange_order_id=order.exchange_order_id,
    )


async def _dispatch_real_submission(
    session: AsyncSession,
    *,
    order: Order,
    spot_client: SodexSpotClient,
    perps_client: SodexPerpsClient,
    api_key_name_resolver,
) -> SubmitResult:
    """Send the signed payload to the gateway and persist the outcome."""
    from etfpulse.models.user import User

    # Need the user for api_key_name. Order has no user relationship
    # eagerly loaded; one extra select.
    user = (await session.execute(select(User).where(User.id == order.user_id))).scalar_one()

    resolver = api_key_name_resolver or _default_api_key_resolver
    api_key_name = resolver(user, order.venue)

    body_bytes = extract_params_bytes(order.eip712_payload or "")
    signature = order.eip712_signature or ""
    nonce = order.nonce or 0

    try:
        if order.venue == Venue.SODEX_SPOT.value:
            items = await spot_client.submit_batch_new_order(
                body_bytes=body_bytes,
                typed_signature=signature,
                nonce=nonce,
                api_key_name=api_key_name,
            )
        elif order.venue == Venue.SODEX_PERPS.value:
            items = await perps_client.submit_batch_new_order(
                body_bytes=body_bytes,
                typed_signature=signature,
                nonce=nonce,
                api_key_name=api_key_name,
            )
        else:
            raise ValueError(f"_dispatch_real_submission: unknown venue {order.venue!r}")
    except SodexAuthError as exc:
        # PR D.3.1 correction: auth errors are NOT transient — they're
        # deterministic gateway-side decisions ("API key not found",
        # "Failed to recover signer") + the gateway's replay
        # protection makes re-submitting the same nonce+signature
        # structurally impossible. The original D.3 comment claimed
        # transience and left the order SUBMITTED for reconcile to
        # resolve, but there is no reconcile path that recovers an
        # auth-rejected submit: the signed bytes never reach the
        # matching engine. Treat as terminal REJECTED so the order
        # exits the active-order set and the user can re-prepare
        # (which generates a fresh nonce) after fixing the underlying
        # api-key binding via D.4's wallet-bind flow.
        #
        # PR D.3.2 note: `Order.eip712_signature` is intentionally
        # preserved on the REJECTED row as audit trail of the bytes
        # that were tendered to the gateway. A future operator
        # reading the table will see "REJECTED + signature attached
        # + raw_response indicates auth failure" — full picture for
        # incident review.
        order.status = OrderStatus.REJECTED.value
        order.error_message = f"auth: {exc}"[:500]
        order.raw_response = {"error": "auth", "detail": str(exc)}
        await session.flush()
        log.warning(
            "execution_submit_new_auth_rejected",
            order_id=order.id,
            error=str(exc),
            note="auth errors are terminal — re-prepare with fresh nonce after fixing api-key",
        )
        return SubmitResult(
            status=order.status,
            order_id=order.id,
            error_message=order.error_message,
        )
    except SodexEnvelopeError as exc:
        # Outer envelope validation error — terminal. Order REJECTED.
        order.status = OrderStatus.REJECTED.value
        order.error_message = f"envelope: {exc}"[:500]
        order.raw_response = {"error": "envelope", "detail": str(exc)}
        await session.flush()
        log.warning(
            "execution_submit_new_envelope_rejected",
            order_id=order.id,
            error=str(exc),
        )
        return SubmitResult(
            status=order.status,
            order_id=order.id,
            error_message=order.error_message,
        )
    # Any other exception (HTTP timeout, parse error) bubbles up; route
    # rolls back the tx, leaving the order at PENDING for retry.

    # Defensive: we submit batch-of-1; expect exactly one item.
    if len(items) != 1:
        order.raw_response = {"error": "unexpected_item_count", "items": len(items)}
        await session.flush()
        return SubmitResult(
            status=order.status,
            order_id=order.id,
            error_message=f"unexpected gateway response: {len(items)} items",
        )

    item = items[0]
    order.raw_response = {"item_code": item.code, "order_id": item.order_id}
    if item.code == 0:
        # ACK from gateway. order_id is on the venue.
        order.status = OrderStatus.ACKED.value
        order.exchange_order_id = str(item.order_id) if item.order_id else None
    else:
        # Per-order rejection — terminal.
        order.status = OrderStatus.REJECTED.value
        order.error_message = (item.error or "rejected_unknown_reason")[:500]

    await session.flush()
    log.info(
        "execution_submit_new_complete",
        order_id=order.id,
        status=order.status,
        venue_order_id=order.exchange_order_id,
    )
    return SubmitResult(
        status=order.status,
        order_id=order.id,
        exchange_order_id=order.exchange_order_id,
        error_message=order.error_message,
    )


async def _dispatch_real_cancel(
    session: AsyncSession,
    *,
    order: Order,
    spot_client: SodexSpotClient,
    perps_client: SodexPerpsClient,
    api_key_name_resolver,
) -> SubmitResult:
    from etfpulse.models.user import User

    user = (await session.execute(select(User).where(User.id == order.user_id))).scalar_one()
    resolver = api_key_name_resolver or _default_api_key_resolver
    api_key_name = resolver(user, order.venue)

    body_bytes = extract_params_bytes(order.cancel_eip712_payload or "")
    signature = order.cancel_eip712_signature or ""
    cancel_nonce = order.cancel_nonce or 0

    try:
        if order.venue == Venue.SODEX_SPOT.value:
            items = await spot_client.submit_batch_cancel_order(
                body_bytes=body_bytes,
                typed_signature=signature,
                nonce=cancel_nonce,
                api_key_name=api_key_name,
            )
        elif order.venue == Venue.SODEX_PERPS.value:
            items = await perps_client.submit_batch_cancel_order(
                body_bytes=body_bytes,
                typed_signature=signature,
                nonce=cancel_nonce,
                api_key_name=api_key_name,
            )
        else:
            raise ValueError(f"_dispatch_real_cancel: unknown venue {order.venue!r}")
    except SodexAuthError as exc:
        order.cancel_error_message = f"auth: {exc}"[:500]
        await session.flush()
        return SubmitResult(
            status=order.status,
            order_id=order.id,
            error_message=order.cancel_error_message,
        )
    except SodexEnvelopeError as exc:
        order.cancel_error_message = f"envelope: {exc}"[:500]
        await session.flush()
        return SubmitResult(
            status=order.status,
            order_id=order.id,
            error_message=order.cancel_error_message,
        )

    if len(items) != 1:
        order.cancel_error_message = f"unexpected gateway response: {len(items)} items"
        await session.flush()
        return SubmitResult(
            status=order.status,
            order_id=order.id,
            error_message=order.cancel_error_message,
        )

    item = items[0]
    if item.code == 0:
        # Cancel accepted. Order.status moves to CANCELLED. filled_size
        # from any partial fill is preserved.
        order.status = OrderStatus.CANCELLED.value
    else:
        # Cancel rejected (common: OrderNotFound when the order filled
        # in the race window). Order.status UNCHANGED; cancel_error_message
        # set.
        order.cancel_error_message = (item.error or "cancel_rejected_unknown")[:500]
    await session.flush()
    log.info(
        "execution_submit_cancel_complete",
        order_id=order.id,
        status=order.status,
        error=order.cancel_error_message,
    )
    return SubmitResult(
        status=order.status,
        order_id=order.id,
        error_message=order.cancel_error_message,
    )


# ---------------------------------------------------------------------------
# Internals — small helpers
# ---------------------------------------------------------------------------


async def _load_order_or_raise(session: AsyncSession, order_id: int) -> Order:
    order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if order is None:
        raise ValueError(f"Order id={order_id} does not exist")
    return order


async def _reject_submission(
    *,
    session: AsyncSession,
    order: Order,
    error: str,
    log_event: str,
) -> SubmitResult:
    """Helper for the few sync-rejection paths in submit_new. Logs +
    persists `error_message` + transitions to REJECTED.

    PR D.3.4 — flushes for parity with the gateway-side rejection
    paths in `_dispatch_real_submission` (lines ~1026/1043/1060),
    which all explicitly `await session.flush()` before returning.
    Without the flush, any future caller that doesn't commit
    immediately after this helper would lose the REJECTED state.
    """
    order.status = OrderStatus.REJECTED.value
    order.error_message = error
    await session.flush()
    log.warning(log_event, order_id=order.id, error=error)
    return SubmitResult(
        status=order.status,
        order_id=order.id,
        error_message=error,
    )


__all__ = [
    "PrepareResult",
    "SubmitResult",
    "prepare_new",
    "submit_new",
    "prepare_cancel",
    "submit_cancel",
]
