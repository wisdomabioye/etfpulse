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
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


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
    eip712_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Typed signature: '0x01' (SoDEX type byte) + 65-byte ECDSA signature.
    # Format = 2 (`0x`) + 2 (`01`) + 130 hex chars = 134 chars; String(140)
    # leaves 6 chars headroom. Stored lowercased (CHECK enforces) so the
    # gateway's case-normalisation behaviour can't cause a stored-vs-replayed
    # mismatch.
    eip712_signature: Mapped[str | None] = mapped_column(String(140), nullable=True)

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
        # Reaper scan path. Predicate-aligned with the reaper query so
        # Postgres can index-only-scan the candidate set instead of
        # filtering the full table.
        #
        # COUPLING: the status literals `'pending'`, `'submitted'`, `'acked'`
        # below MUST match `OrderStatus` enum values. If those values are
        # renamed in `OrderStatus`, this predicate goes silently stale —
        # the partial index would only cover rows whose status matched the
        # OLD literal, and the reaper would miss new-name rows. Postgres
        # does NOT validate partial-index predicates against enum changes.
        # Anti-drift: any rename to OrderStatus must come with a migration
        # that drops and recreates this index with the new literals.
        Index(
            "ix_orders_expires",
            "nonce_expires_at",
            postgresql_where=text(
                "nonce_expires_at IS NOT NULL AND status IN ('pending','submitted','acked')"
            ),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} asset={self.asset} side={self.side} "
            f"type={self.order_type} status={self.status}>"
        )
