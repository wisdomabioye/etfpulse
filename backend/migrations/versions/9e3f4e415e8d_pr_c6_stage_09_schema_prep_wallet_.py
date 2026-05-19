"""pr c6 stage 09 schema prep — wallet + status + venue + eip712

Revision ID: 9e3f4e415e8d
Revises: 6ad6b15e7f00
Create Date: 2026-05-19 11:22:13.885195

Stage 09 schema preparation. Five distinct concerns in one revision because
they're a single conceptual change (the execution-surface schema becoming
real):

1. `users.wallet_address` + format CHECK + partial-unique index — column
   needed by D.4's wallet-connect route.
2. `users.tier` / `users.tier_expires_at` + `telegram_groups.tier` /
   `telegram_groups.tier_expires_at` — DROP. Vestigial premium-tier scaffolding
   from the buildathon era; product is one-tier (see CLAUDE.md "Subscription
   model" + memory).
3. `orders.status` CHECK — replace 5-value with 8-value list (adds
   submitted, acked, expired for the EIP-712 lifecycle).
4. `orders.venue` + `positions.venue` CHECKs — venue split: was
   unconstrained `String(20)` accepting any value; now restricted to
   `'sodex_spot'` | `'sodex_perps'`.
5. Seven new `orders.*` columns for EIP-712 signing: account_id, symbol_id,
   nonce, nonce_expires_at, expires_at, eip712_payload, eip712_signature
   + three integrity CHECKs + two partial indexes.

Precondition guards (run FIRST inside upgrade): we refuse to silently
destroy non-default tier data or accept rows that would violate the new
CHECK constraints. The migration aborts with a clear message if any of
the four asserted invariants fail.

Rollback contract (per CLAUDE.md issue #22): downgrade reverses every
upgrade step. The re-added `tier` columns get `server_default='free'` so
existing rows survive the NOT NULL constraint. `wallet_address` is data-
lossy on downgrade (any bound wallets are dropped) — documented limit of
schema-only rollback.

Deploy-safety assumption: this revision DROPS columns (`users.tier`,
`telegram_groups.tier*`) in the same revision as the code that stopped
reading them. ETFPulse is single-process / stop-and-start on Coolify,
not rolling, so an N-1 worker reading the dropped column is impossible.
If the topology ever moves to rolling deploys, this revision MUST be
split into two: (a) code-only drop of references; (b) one deploy cycle
later, DROP COLUMN. See CLAUDE.md "Migrations" section.
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9e3f4e415e8d"
down_revision: str | None = "6ad6b15e7f00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Constants -------------------------------------------------------------

_NEW_ORDER_STATUSES = (
    "pending",
    "submitted",
    "acked",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "expired",
)
_OLD_ORDER_STATUSES = (
    "pending",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
)
_NEW_VENUES = ("sodex_spot", "sodex_perps")


_SAFE_ENUM_LITERAL = re.compile(r"^[a-z_]+$")


def _quoted_csv(values: tuple[str, ...]) -> str:
    """Render a tuple of enum-literal strings as a SQL-quoted CSV list.

    Defensive guard: every value must match `^[a-z_]+$` (ASCII lowercase +
    underscore only). This matches the actual enum values we use
    (`pending`, `sodex_spot`, etc.) and rejects anything that could
    smuggle SQL syntax — quotes, backslashes, semicolons, parentheses,
    even Unicode letters that `str.isalpha()` would silently accept.
    """
    for v in values:
        if not _SAFE_ENUM_LITERAL.fullmatch(v):
            raise ValueError(
                f"_quoted_csv: value {v!r} is not a safe enum literal "
                "(expected ASCII lowercase + underscore only)"
            )
    return ",".join(f"'{v}'" for v in values)


# --- Precondition guards ----------------------------------------------------


def _assert_no_nondefault_tier() -> None:
    """Refuse to drop `tier` if any row in users/groups carries non-default.

    Rationale: DROP COLUMN is irreversible at the data level. If anyone in
    prod set tier='premium' (or anything else), losing that silently is a
    data-loss event. Fail loudly so an operator can rescue it first.
    """
    bind = op.get_bind()
    for table in ("users", "telegram_groups"):
        result = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE tier IS NOT NULL AND tier <> 'free'")
        ).scalar_one()
        if result:
            raise RuntimeError(
                f"Migration aborted: {table} has {result} row(s) with non-default tier. "
                "Inspect/export before re-running the migration."
            )


def _assert_orders_venue_valid() -> None:
    """Refuse to add Orders.venue CHECK if any existing row would violate it."""
    bind = op.get_bind()
    result = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM orders WHERE venue NOT IN ({_quoted_csv(_NEW_VENUES)})")
    ).scalar_one()
    if result:
        raise RuntimeError(
            f"Migration aborted: orders has {result} row(s) with venue not in "
            f"{_NEW_VENUES}. Inspect and re-classify before re-running."
        )


def _assert_positions_venue_valid() -> None:
    bind = op.get_bind()
    result = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM positions WHERE venue NOT IN ({_quoted_csv(_NEW_VENUES)})")
    ).scalar_one()
    if result:
        raise RuntimeError(
            f"Migration aborted: positions has {result} row(s) with venue not in "
            f"{_NEW_VENUES}. Inspect and re-classify before re-running."
        )


def _assert_orders_status_valid() -> None:
    """Pre-check: no orders.status outside the expanded 8-value list.

    All historical Stage-04..08 rows used the original 5 values which are
    a strict subset of the new 8, so this should always be 0. The guard
    catches a future world where a dev pushed an out-of-band status.
    """
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM orders WHERE status NOT IN ({_quoted_csv(_NEW_ORDER_STATUSES)})"
        )
    ).scalar_one()
    if result:
        raise RuntimeError(
            f"Migration aborted: orders has {result} row(s) with status outside "
            f"{_NEW_ORDER_STATUSES}. Investigate before re-running."
        )


# --- Upgrade ----------------------------------------------------------------


def upgrade() -> None:
    # 1) Preconditions first. If any of these fail, we abort BEFORE
    #    any structural change so retry is just "fix the data + rerun."
    _assert_no_nondefault_tier()
    _assert_orders_venue_valid()
    _assert_positions_venue_valid()
    _assert_orders_status_valid()

    # 2) Orders — add the seven EIP-712 lifecycle columns. All nullable;
    #    pre-Stage-09 rows + paper-trade rows stay NULL.
    op.add_column("orders", sa.Column("account_id", sa.BigInteger(), nullable=True))
    op.add_column("orders", sa.Column("symbol_id", sa.BigInteger(), nullable=True))
    op.add_column("orders", sa.Column("nonce", sa.BigInteger(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("nonce_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("orders", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "orders",
        sa.Column("eip712_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("orders", sa.Column("eip712_signature", sa.String(length=140), nullable=True))

    # 3) Orders — replace the status CHECK (5 → 8 values).
    op.drop_constraint("ck_orders_status_enum", "orders", type_="check")
    op.create_check_constraint(
        "ck_orders_status_enum",
        "orders",
        f"status IN ({_quoted_csv(_NEW_ORDER_STATUSES)})",
    )

    # 4) Orders — new CHECKs (venue + EIP-712 invariants).
    op.create_check_constraint(
        "ck_orders_venue_enum",
        "orders",
        f"venue IN ({_quoted_csv(_NEW_VENUES)})",
    )
    op.create_check_constraint(
        "ck_orders_nonce_consistency",
        "orders",
        "(nonce IS NULL) = (nonce_expires_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_orders_signature_requires_payload",
        "orders",
        "eip712_signature IS NULL OR eip712_payload IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_orders_signature_format",
        "orders",
        "eip712_signature IS NULL OR eip712_signature ~ '^0x01[0-9a-f]{130}$'",
    )

    # 5) Orders — new partial indexes for reconciliation + reaper.
    op.create_index(
        "ix_orders_nonce",
        "orders",
        ["user_id", "nonce"],
        unique=False,
        postgresql_where=sa.text("nonce IS NOT NULL"),
    )
    op.create_index(
        "ix_orders_expires",
        "orders",
        ["nonce_expires_at"],
        unique=False,
        postgresql_where=sa.text(
            "nonce_expires_at IS NOT NULL AND status IN ('pending','submitted','acked')"
        ),
    )

    # 6) Positions — new venue CHECK (column was previously unconstrained).
    op.create_check_constraint(
        "ck_positions_venue_enum",
        "positions",
        f"venue IN ({_quoted_csv(_NEW_VENUES)})",
    )

    # 7) Users — add wallet_address + format CHECK + partial-unique index.
    op.add_column("users", sa.Column("wallet_address", sa.String(length=42), nullable=True))
    op.create_check_constraint(
        "ck_users_wallet_address_format",
        "users",
        "wallet_address IS NULL OR wallet_address ~ '^0x[0-9a-f]{40}$'",
    )
    op.create_index(
        "ix_users_wallet_address",
        "users",
        ["wallet_address"],
        unique=True,
        postgresql_where=sa.text("wallet_address IS NOT NULL"),
    )

    # 8) Users + groups — drop tier scaffolding.
    op.drop_index("ix_users_tier", table_name="users")
    op.drop_column("users", "tier_expires_at")
    op.drop_column("users", "tier")

    # 9) Telegram groups — rebuild ix_groups_delivery without `tier`,
    #    drop tier columns.
    op.drop_index("ix_groups_delivery", table_name="telegram_groups")
    op.create_index(
        "ix_groups_delivery",
        "telegram_groups",
        ["is_active", "pref_paused", "pref_min_confidence"],
        unique=False,
    )
    op.drop_column("telegram_groups", "tier_expires_at")
    op.drop_column("telegram_groups", "tier")


# --- Downgrade --------------------------------------------------------------


def downgrade() -> None:
    # Reverse in exact opposite order so each drop has its predecessor still
    # in place.

    # 9) Re-add tier columns on telegram_groups (with server_default for
    #    NOT NULL safety on existing rows).
    op.add_column(
        "telegram_groups",
        sa.Column(
            "tier",
            sa.VARCHAR(length=20),
            nullable=False,
            server_default="free",
        ),
    )
    op.add_column(
        "telegram_groups",
        sa.Column(
            "tier_expires_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.drop_index("ix_groups_delivery", table_name="telegram_groups")
    op.create_index(
        "ix_groups_delivery",
        "telegram_groups",
        ["is_active", "tier", "pref_paused"],
        unique=False,
    )

    # 8) Re-add tier on users + the tier index.
    op.add_column(
        "users",
        sa.Column(
            "tier",
            sa.VARCHAR(length=20),
            nullable=False,
            server_default="free",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "tier_expires_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.create_index("ix_users_tier", "users", ["tier", "tier_expires_at"], unique=False)

    # 7) Drop wallet_address + its CHECK + its partial-unique index.
    op.drop_index(
        "ix_users_wallet_address",
        table_name="users",
        postgresql_where=sa.text("wallet_address IS NOT NULL"),
    )
    op.drop_constraint("ck_users_wallet_address_format", "users", type_="check")
    op.drop_column("users", "wallet_address")

    # 6) Drop positions.venue CHECK.
    op.drop_constraint("ck_positions_venue_enum", "positions", type_="check")

    # 5) Drop new partial indexes on orders.
    op.drop_index(
        "ix_orders_expires",
        table_name="orders",
        postgresql_where=sa.text(
            "nonce_expires_at IS NOT NULL AND status IN ('pending','submitted','acked')"
        ),
    )
    op.drop_index(
        "ix_orders_nonce",
        table_name="orders",
        postgresql_where=sa.text("nonce IS NOT NULL"),
    )

    # 4) Drop the new CHECKs on orders.
    op.drop_constraint("ck_orders_signature_format", "orders", type_="check")
    op.drop_constraint("ck_orders_signature_requires_payload", "orders", type_="check")
    op.drop_constraint("ck_orders_nonce_consistency", "orders", type_="check")
    op.drop_constraint("ck_orders_venue_enum", "orders", type_="check")

    # 3) Restore old status CHECK. Any row using submitted/acked/expired
    #    becomes invalid against the old constraint — emit a sentinel that
    #    forces the operator to clean these up before downgrade succeeds.
    bind = op.get_bind()
    stranded = bind.execute(
        sa.text("SELECT COUNT(*) FROM orders WHERE status IN ('submitted','acked','expired')")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"Downgrade aborted: orders has {stranded} row(s) with status in "
            "(submitted, acked, expired) — these are unrepresentable under the "
            "old 5-value CHECK. Re-classify to pending/cancelled/rejected first."
        )
    op.drop_constraint("ck_orders_status_enum", "orders", type_="check")
    op.create_check_constraint(
        "ck_orders_status_enum",
        "orders",
        f"status IN ({_quoted_csv(_OLD_ORDER_STATUSES)})",
    )

    # 2) Drop the seven Stage 09 columns from orders.
    op.drop_column("orders", "eip712_signature")
    op.drop_column("orders", "eip712_payload")
    op.drop_column("orders", "expires_at")
    op.drop_column("orders", "nonce_expires_at")
    op.drop_column("orders", "nonce")
    op.drop_column("orders", "symbol_id")
    op.drop_column("orders", "account_id")
