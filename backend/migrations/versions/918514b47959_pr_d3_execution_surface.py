"""pr d3 execution surface

Revision ID: 918514b47959
Revises: 9e3f4e415e8d
Create Date: 2026-05-20 16:54:50.092650

PR D.3 schema preparation. Six conceptual changes in one revision because
they collectively become "the execution surface is real":

1. New `sodex_symbols` table — daily-refreshed cache of venue listings so
   `prepare_order` resolves asset → symbol_id without a per-call HTTP
   round-trip.
2. `users` — adds `paper_trade` (operator-set per-user flag),
   `sodex_account_id` (cached venue accountID, V.3-stable per wallet),
   and `sodex_{spot,perps}_api_key_name` (per-venue X-API-Key NAME).
3. `circuit_breakers` — adds `user_id` (NULL = global, non-NULL = per-user
   halt) + partial index for the risk-controller scan path; extends
   `trigger_type` CHECK to admit `daily_loss_limit`.
4. `orders` — JSONB `eip712_payload` → TEXT. D.1's `TypedDataBundle`
   contract requires byte-exact persistence; JSONB normalises whitespace
   and re-quotes numerics, silently breaking the signed payload contract.
   Precondition: zero non-NULL `eip712_payload` rows (Stage 09 hasn't
   shipped). Adds `eip712_payload_hash`, `is_conditional`, and the full
   cancel-lifecycle column set (5 columns + 4 CHECKs).
5. `positions` — adds `leverage` (perps only; NULL on spot) + CHECK.
6. CHECKs for every new constrained column.

Precondition guards (run FIRST inside upgrade): we assert the inherited
C.6 column `orders.eip712_payload` has zero non-NULL rows before the
JSONB→TEXT conversion. This is correct under the deploy-safety
assumption (Stage 09 hasn't shipped + no production rows), but we make
that assumption EXPLICIT so a future timeline where the order survives
out of order doesn't silently corrupt signed payloads.

Rollback contract: downgrade reverses every upgrade step. The TEXT→JSONB
re-tightening can lose byte-exactness even for rows that existed when
the column was TEXT (whitespace normalisation), so downgrade asserts
zero non-NULL rows before performing the type change — same posture as
upgrade.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "918514b47959"
down_revision: str | None = "9e3f4e415e8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Constants -------------------------------------------------------------

_NEW_CB_TRIGGERS = ("macro_event", "manual", "daily_loss_limit")
_OLD_CB_TRIGGERS = ("macro_event", "manual")
_VENUES = ("sodex_spot", "sodex_perps")


def _quoted_csv(values: tuple[str, ...]) -> str:
    """SQL-quoted CSV of safe enum literals. Same pattern as the C.6
    migration — defensive against any literal that smuggles SQL syntax."""
    import re

    safe = re.compile(r"^[a-z_]+$")
    for v in values:
        if not safe.fullmatch(v):
            raise ValueError(f"_quoted_csv: unsafe literal {v!r}")
    return ",".join(f"'{v}'" for v in values)


# --- Precondition guards ----------------------------------------------------


def _assert_no_eip712_payload_rows() -> None:
    """Refuse the JSONB→TEXT conversion if any row has a non-NULL payload.

    JSONB→TEXT is byte-lossy: PostgreSQL stores JSONB in a binary form
    that has already discarded the original whitespace + key ordering of
    the source bytes. Casting to TEXT yields a re-serialised form that
    will NOT match what the wallet originally signed. Stage 09 hasn't
    shipped, so this should always pass. The guard is here so a future
    out-of-order deploy fails loud instead of silently corrupting the
    `(payload_hash, signature)` invariant.
    """
    bind = op.get_bind()
    n = bind.execute(
        sa.text("SELECT COUNT(*) FROM orders WHERE eip712_payload IS NOT NULL")
    ).scalar_one()
    if n:
        raise RuntimeError(
            f"Migration aborted: orders has {n} row(s) with non-NULL eip712_payload. "
            "JSONB→TEXT conversion is byte-lossy for stored payloads; signature "
            "verification would break post-migration. Inspect/export these rows "
            "before re-running."
        )


# --- Upgrade ----------------------------------------------------------------


def upgrade() -> None:
    # 0) Preconditions first.
    _assert_no_eip712_payload_rows()

    # 1) New table: sodex_symbols (D.3 symbol cache).
    op.create_table(
        "sodex_symbols",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("venue", sa.String(length=20), nullable=False),
        sa.Column("symbol_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("asset", sa.String(length=10), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"venue IN ({_quoted_csv(_VENUES)})",
            name="ck_sodex_symbols_venue_enum",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sodex_symbols")),
        sa.UniqueConstraint("venue", "name", name="uq_sodex_symbols_venue_name"),
    )
    op.create_index(
        "ix_sodex_symbols_venue_asset",
        "sodex_symbols",
        ["venue", "asset"],
        unique=False,
    )

    # 2) Users — new D.3 columns. NOT NULL `paper_trade` with default; other
    #    three nullable (populated by D.4 wallet-bind flow).
    op.add_column("users", sa.Column("sodex_account_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "users", sa.Column("sodex_spot_api_key_name", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "users", sa.Column("sodex_perps_api_key_name", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column("paper_trade", sa.Boolean(), server_default="false", nullable=False),
    )

    # 3) Circuit breakers — per-user dimension + trigger_type enum extension.
    op.add_column("circuit_breakers", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_circuit_breakers_user_id_users"),
        "circuit_breakers",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_circuit_breakers_user_unresolved",
        "circuit_breakers",
        ["user_id", "trigger_type"],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    # Replace trigger_type CHECK (admit `daily_loss_limit`).
    op.drop_constraint("ck_circuit_breakers_trigger_type_enum", "circuit_breakers", type_="check")
    op.create_check_constraint(
        "ck_circuit_breakers_trigger_type_enum",
        "circuit_breakers",
        f"trigger_type IN ({_quoted_csv(_NEW_CB_TRIGGERS)})",
    )

    # 4) Orders — JSONB→TEXT on eip712_payload (D.1 byte-exact contract).
    #    Guarded above. `postgresql_using` cast keeps the alembic op
    #    explicit even though we expect zero rows.
    op.alter_column(
        "orders",
        "eip712_payload",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using="eip712_payload::text",
    )

    # 5) Orders — new columns: payload_hash + 6 cancel-lifecycle + is_conditional.
    op.add_column("orders", sa.Column("eip712_payload_hash", sa.String(length=66), nullable=True))
    op.add_column("orders", sa.Column("cancel_nonce", sa.BigInteger(), nullable=True))
    op.add_column("orders", sa.Column("cancel_eip712_payload", sa.Text(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("cancel_eip712_payload_hash", sa.String(length=66), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("cancel_eip712_signature", sa.String(length=140), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("cancel_submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("cancel_error_message", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column(
            "is_conditional",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )

    # 6) Orders — new CHECKs for the new columns.
    op.create_check_constraint(
        "ck_orders_eip712_payload_hash_format",
        "orders",
        "eip712_payload_hash IS NULL OR eip712_payload_hash ~ '^0x[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_orders_cancel_nonce_consistency",
        "orders",
        "(cancel_nonce IS NULL) = (cancel_eip712_payload IS NULL)",
    )
    op.create_check_constraint(
        "ck_orders_cancel_signature_requires_payload",
        "orders",
        "cancel_eip712_signature IS NULL OR cancel_eip712_payload IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_orders_cancel_signature_format",
        "orders",
        "cancel_eip712_signature IS NULL OR cancel_eip712_signature ~ '^0x01[0-9a-f]{130}$'",
    )
    op.create_check_constraint(
        "ck_orders_cancel_payload_hash_format",
        "orders",
        "cancel_eip712_payload_hash IS NULL OR cancel_eip712_payload_hash ~ '^0x[0-9a-f]{64}$'",
    )

    # 7) Positions — leverage column + CHECK.
    op.add_column(
        "positions",
        sa.Column("leverage", sa.Numeric(precision=8, scale=2), nullable=True),
    )
    op.create_check_constraint(
        "ck_positions_leverage_positive",
        "positions",
        "leverage IS NULL OR leverage > 0",
    )


# --- Downgrade --------------------------------------------------------------


def downgrade() -> None:
    # Symmetric reversal. The TEXT→JSONB step is byte-lossy in the same
    # way the upgrade's JSONB→TEXT was; guard symmetrically.

    # 7) Positions — drop leverage CHECK + column.
    op.drop_constraint("ck_positions_leverage_positive", "positions", type_="check")
    op.drop_column("positions", "leverage")

    # 6) Orders — drop the five new CHECKs.
    op.drop_constraint("ck_orders_cancel_payload_hash_format", "orders", type_="check")
    op.drop_constraint("ck_orders_cancel_signature_format", "orders", type_="check")
    op.drop_constraint("ck_orders_cancel_signature_requires_payload", "orders", type_="check")
    op.drop_constraint("ck_orders_cancel_nonce_consistency", "orders", type_="check")
    op.drop_constraint("ck_orders_eip712_payload_hash_format", "orders", type_="check")

    # 5) Orders — drop the new columns (reverse order of upgrade).
    op.drop_column("orders", "is_conditional")
    op.drop_column("orders", "cancel_error_message")
    op.drop_column("orders", "cancel_submitted_at")
    op.drop_column("orders", "cancel_eip712_signature")
    op.drop_column("orders", "cancel_eip712_payload_hash")
    op.drop_column("orders", "cancel_eip712_payload")
    op.drop_column("orders", "cancel_nonce")
    op.drop_column("orders", "eip712_payload_hash")

    # 4) Orders — TEXT→JSONB on eip712_payload. Guard against non-NULL rows.
    bind = op.get_bind()
    n = bind.execute(
        sa.text("SELECT COUNT(*) FROM orders WHERE eip712_payload IS NOT NULL")
    ).scalar_one()
    if n:
        raise RuntimeError(
            f"Downgrade aborted: orders has {n} row(s) with non-NULL eip712_payload. "
            "TEXT→JSONB conversion is byte-lossy; signature verification would "
            "break post-downgrade. Inspect/export before re-running."
        )
    op.alter_column(
        "orders",
        "eip712_payload",
        existing_type=sa.Text(),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=True,
        postgresql_using="eip712_payload::jsonb",
    )

    # 3) Circuit breakers — restore old trigger_type CHECK + drop index + FK + column.
    #    Guard: any rows using the new `daily_loss_limit` value would be
    #    unrepresentable under the old CHECK.
    stranded = bind.execute(
        sa.text("SELECT COUNT(*) FROM circuit_breakers WHERE trigger_type = 'daily_loss_limit'")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"Downgrade aborted: circuit_breakers has {stranded} row(s) with "
            "trigger_type='daily_loss_limit' — unrepresentable under the old CHECK. "
            "Re-classify or delete before re-running."
        )
    op.drop_constraint("ck_circuit_breakers_trigger_type_enum", "circuit_breakers", type_="check")
    op.create_check_constraint(
        "ck_circuit_breakers_trigger_type_enum",
        "circuit_breakers",
        f"trigger_type IN ({_quoted_csv(_OLD_CB_TRIGGERS)})",
    )
    op.drop_index(
        "ix_circuit_breakers_user_unresolved",
        table_name="circuit_breakers",
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.drop_constraint(
        op.f("fk_circuit_breakers_user_id_users"),
        "circuit_breakers",
        type_="foreignkey",
    )
    op.drop_column("circuit_breakers", "user_id")

    # 2) Users — drop the four D.3 columns.
    op.drop_column("users", "paper_trade")
    op.drop_column("users", "sodex_perps_api_key_name")
    op.drop_column("users", "sodex_spot_api_key_name")
    op.drop_column("users", "sodex_account_id")

    # 1) Sodex symbols — drop index + table.
    op.drop_index("ix_sodex_symbols_venue_asset", table_name="sodex_symbols")
    op.drop_table("sodex_symbols")
