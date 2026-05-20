from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class OrderStatus(StrEnum):
    """Lifecycle states for a SoDEX order, in chronological progression.

    PENDING — row inserted, EIP-712 typed-data built, awaiting client signature
              (wallet-side via wagmi/viem). Reaper auto-EXPIRES if nonce window
              passes without a signature.
    SUBMITTED — signature received, request POSTed to SoDEX gateway, awaiting
                ack. If the gateway returns a synchronous error, transitions
                straight to REJECTED.
    ACKED — gateway returned a 2xx with `exchange_order_id`; order is live on
            the SoDEX book. Awaiting fills via WebSocket / reconciliation.
    PARTIALLY_FILLED, FILLED — fill states reported by the venue.
    CANCELLED — user-initiated cancel succeeded (DELETE /trade/orders).
    REJECTED — gateway refused (validation, insufficient balance, etc.).
    EXPIRED — nonce window passed without ack, or order TTL hit via the
              user-supplied `expires_at` deadline. Terminal.

    Order matters: any state change MUST progress monotonically through this
    enum. The reaper + reconciliation paths assume terminal states
    (FILLED, CANCELLED, REJECTED, EXPIRED) are never re-opened.
    """

    PENDING = "pending"
    SUBMITTED = "submitted"
    ACKED = "acked"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Terminal states — used by reaper guards + reconciliation to short-circuit.
TERMINAL_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    """DB-stored TIF values for `Order.time_in_force` (VARCHAR(10) column,
    no CHECK constraint — extension here is a pure code change).

    Mapping to SoDEX gateway IntEnum values (`adapters.sodex.schemas.TimeInForce`):
        GTC = "gtc" ↔ IntEnum 1
        IOC = "ioc" ↔ IntEnum 3
        FOK = "fok" ↔ IntEnum 2  (gateway docs: "not supported yet")
        GTX = "gtx" ↔ IntEnum 4  (post-only)
    """

    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    GTX = "gtx"  # post-only — perps only on SoDEX


class Venue(StrEnum):
    """Execution venue. Shared with Position — orders and positions live on the same venue.

    SoDEX is split into two distinct gateways with different EIP-712 domain
    names (`spot` / `futures`), different chainIds (286623 / 138565 testnet),
    and different request schemas. Treating them as one venue obscured this
    pre-Stage-09; the split was deferred from C.1 and lands here. The
    pre-existing literal `"sodex"` is REMOVED — no production rows
    referenced it (Stage 09 hasn't shipped).
    """

    SODEX_SPOT = "sodex_spot"
    SODEX_PERPS = "sodex_perps"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    venue: Mapped[str] = mapped_column(String(20), nullable=False)
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(10), nullable=False)
    # Sizes use Numeric(28, 18) — generous scale covers wei-denominated quantities
    # on EVM venues without forcing tokens to round at submission. Prices /
    # fiat-denominated values use Numeric(18, 8) — 8 decimals matches Binance
    # / SoSoValue conventions.
    requested_size: Mapped[Decimal] = mapped_column(Numeric(28, 18), nullable=False)
    requested_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    filled_size: Mapped[Decimal | None] = mapped_column(Numeric(28, 18), nullable=True)
    filled_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    filled_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    fees: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.PENDING, nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Paper-trade flag (#64): True = simulated, no on-chain submission. Defaults
    # False so any code path that omits the kwarg gets the safe real-execution
    # path's CHECK / circuit-breaker treatment. Operators flip to True per-user
    # via a future admin toggle; Stage 09 wires the executor to this flag.
    paper_trade: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Outbound request payload captured at submission time. Distinct from
    # raw_response (the venue's reply) so a debug session has the full
    # before/after pair. NULL on legacy rows + on paper trades where no
    # request is built.
    raw_request: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ---- Stage 09 SoDEX EIP-712 lifecycle columns (PR C.6) ----
    #
    # All seven are NULL on pre-Stage-09 rows. For Stage 09 rows the build-
    # time path (D.4 route) populates: account_id, symbol_id, nonce,
    # nonce_expires_at, eip712_payload — and optionally expires_at. The
    # sign-time path (frontend POST after wallet signature) populates:
    # eip712_signature only, then transitions PENDING → SUBMITTED.
    #
    # Invariants enforced by the CHECK constraints below:
    #   - nonce ⇔ nonce_expires_at (both set or both NULL — derived together)
    #   - eip712_signature set ⇒ eip712_payload set (signature without
    #     payload would be unverifiable)
    #   - eip712_signature shape: '0x01' + 130 lowercase hex chars

    # SoDEX `accountID` — separate identity from our `user_id` (one user may
    # hold multiple SoDEX accounts under the same wallet, per api.md).
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Venue's numeric symbol ID, cached from GET /markets/symbols. The order
    # request signs against the ID, not the human ticker (vBTC_vUSDC etc).
    symbol_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # EIP-712 nonce. Per api.md, gateway accepts nonces within (T-2d, T+1d)
    # in milliseconds. We persist the millisecond value used at signing time
    # so reconciliation can recover an order from gateway logs.
    nonce: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Protocol-side nonce-window deadline = (nonce_ms + 1 day). Reaper
    # auto-EXPIRES PENDING/SUBMITTED rows past this point: once the gateway
    # rejects on stale nonce, retry is impossible without rebuilding the
    # whole typed-data and re-prompting the wallet.
    nonce_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # User-supplied "cancel this order if not filled by X." Independent of
    # nonce_expires_at — the user may want a 1-hour TTL on a limit order
    # whose nonce window is 24h. Drives a future scheduleCancel action;
    # today the reaper treats it the same as nonce_expires_at for terminal
    # transitions on PENDING/ACKED rows.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Compact JSON of `{type, params}` — what the gateway re-hashes to verify
    # the signature. Stored verbatim so a reconciliation pass can recompute
    # payloadHash byte-for-byte without re-deriving from other columns
    # (which would be brittle if the SDK's field-order rules drift).
    #
    # PR D.3: **TEXT, not JSONB.** D.1's `TypedDataBundle.payload_json` is a
    # str whose bytes the wallet signs; JSONB would normalise whitespace
    # and re-quote numerics, silently breaking the byte-exact contract.
    # The migration in D.3 converts C.6's JSONB column to TEXT (safe because
    # Stage 09 hasn't shipped; precondition guard asserts zero rows).
    # See anti-drift rule 31 + `pipeline/execution/bytes_helpers.py` for
    # the only sanctioned reader on the HTTP write path.
    eip712_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # PR D.3 — `0x` + 64 lowercase hex chars (keccak256 of `eip712_payload`
    # bytes). Derivable on demand but persisted for O(1) reconciliation
    # lookups + log/debug confirmation that what we stored matches what
    # the wallet signed.
    eip712_payload_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    # Typed signature: '0x01' (SoDEX type byte) + 65-byte ECDSA signature.
    # Format = 2 (`0x`) + 2 (`01`) + 130 hex chars = 134 chars; String(140)
    # leaves 6 chars headroom. Stored lowercased (CHECK enforces) so the
    # gateway's case-normalisation behaviour can't cause a stored-vs-replayed
    # mismatch.
    eip712_signature: Mapped[str | None] = mapped_column(String(140), nullable=True)

    # ---- PR D.3 cancel lifecycle ----
    #
    # A cancel is a SECOND typed-data action against the same Order row.
    # Cancel-of-cancel is impossible; cancel-of-PENDING is local-only (no
    # venue contact, no signature). For ACKED/PARTIALLY_FILLED cancels we
    # persist the cancel's payload + signature so reconciliation can audit
    # by re-hashing the stored bytes.
    #
    # All five columns are NULL on rows whose order was never cancelled.
    # `cancel_error_message` is set when the gateway accepts the cancel
    # request envelope (outer code=0) but rejects the inner item (common:
    # `"order rejected: OrderNotFound"` when the order filled between
    # our load and the cancel landing). In that case Order.status is
    # UNCHANGED — the cancel failed, the order is still live.
    cancel_nonce: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cancel_eip712_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_eip712_payload_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    cancel_eip712_signature: Mapped[str | None] = mapped_column(String(140), nullable=True)
    cancel_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # PR D.3 — conditional-order flag (perps only — stop / trigger orders).
    # ACKED + is_conditional=True means "trigger pending"; the order is
    # accepted by the matching engine but won't fill until the trigger
    # price hits. Reconcile sets this from venue's open-orders response
    # if present. Defaults False; spot orders never set it.
    is_conditional: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # Keep this literal list in sync with `OrderStatus`. Test
        # `test_order_status_enum.py` round-trips every value to catch drift.
        CheckConstraint(
            "status IN ('pending','submitted','acked','partially_filled',"
            "'filled','cancelled','rejected','expired')",
            name="ck_orders_status_enum",
        ),
        # Venue is restricted to the two SoDEX gateways. Mirrors `Venue` enum.
        CheckConstraint(
            "venue IN ('sodex_spot','sodex_perps')",
            name="ck_orders_venue_enum",
        ),
        # Nonce ⇔ nonce_expires_at: both set, or both NULL. Derived
        # together at build time; one without the other is a code bug.
        CheckConstraint(
            "(nonce IS NULL) = (nonce_expires_at IS NULL)",
            name="ck_orders_nonce_consistency",
        ),
        # Signature without payload is unverifiable — gateway would have
        # nothing to re-hash. Closes a malformed-write path.
        CheckConstraint(
            "eip712_signature IS NULL OR eip712_payload IS NOT NULL",
            name="ck_orders_signature_requires_payload",
        ),
        # Signature format: SoDEX type byte (0x01) + 65-byte ECDSA hex.
        # Lowercase hex only — normalised on write.
        CheckConstraint(
            "eip712_signature IS NULL OR eip712_signature ~ '^0x01[0-9a-f]{130}$'",
            name="ck_orders_signature_format",
        ),
        # PR D.3 — eip712_payload_hash format: `0x` + 64 lowercase hex chars.
        CheckConstraint(
            "eip712_payload_hash IS NULL OR eip712_payload_hash ~ '^0x[0-9a-f]{64}$'",
            name="ck_orders_eip712_payload_hash_format",
        ),
        # PR D.3 — cancel-side EIP-712 invariants. Mirror the new-order
        # CHECKs so the cancel branch has the same audit posture.
        CheckConstraint(
            "(cancel_nonce IS NULL) = (cancel_eip712_payload IS NULL)",
            name="ck_orders_cancel_nonce_consistency",
        ),
        CheckConstraint(
            "cancel_eip712_signature IS NULL OR cancel_eip712_payload IS NOT NULL",
            name="ck_orders_cancel_signature_requires_payload",
        ),
        CheckConstraint(
            "cancel_eip712_signature IS NULL OR cancel_eip712_signature ~ '^0x01[0-9a-f]{130}$'",
            name="ck_orders_cancel_signature_format",
        ),
        CheckConstraint(
            "cancel_eip712_payload_hash IS NULL OR cancel_eip712_payload_hash ~ '^0x[0-9a-f]{64}$'",
            name="ck_orders_cancel_payload_hash_format",
        ),
        Index("ix_orders_user", "user_id"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_exchange", "exchange_order_id"),
        Index("ix_orders_signal", "signal_id"),
        # Nonce reverse-lookup: reconciliation pulls a SoDEX webhook event
        # by (user wallet, nonce) — partial because pre-Stage-09 + paper
        # trade rows have NULL nonce, and indexing NULL is wasted space.
        Index(
            "ix_orders_nonce",
            "user_id",
            "nonce",
            postgresql_where=text("nonce IS NOT NULL"),
        ),
        # Reaper scan path. Predicate-aligned with the reaper query
        # (`pipeline/reapers.py:expire_overdue_orders`) so Postgres can
        # index-only-scan the candidate set instead of filtering the full
        # table.
        #
        # PR D.3.1: predicate extended to include `'partially_filled'`.
        # The original D.3 predicate covered only three statuses, but
        # the reaper targets four — PARTIALLY_FILLED rows fell back to
        # seq-scan. Migration `8c61b9480195` re-creates the index with
        # the broader predicate.
        #
        # COUPLING: the status literals below MUST match `OrderStatus`
        # enum values + `pipeline.reapers._OVERDUE_ORDER_STATUSES`. If
        # any of the three drifts, the partial index silently misses
        # rows. Postgres does NOT validate partial-index predicates
        # against enum changes. Anti-drift: any rename to OrderStatus
        # must come with a migration that drops and recreates this
        # index with the new literals.
        Index(
            "ix_orders_expires",
            "nonce_expires_at",
            postgresql_where=text(
                "nonce_expires_at IS NOT NULL "
                "AND status IN ('pending','submitted','acked','partially_filled')"
            ),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} asset={self.asset} side={self.side} "
            f"type={self.order_type} status={self.status}>"
        )
