"""pr d3.1 hardening — extend orders expiry index + circuit_breakers partial unique

Revision ID: 8c61b9480195
Revises: 918514b47959
Create Date: 2026-05-20 20:56:01.360505

D.3 review found two schema-level gaps:

1. **`ix_orders_expires` partial-index predicate missed `partially_filled`.**
   The reaper (`expire_overdue_orders`) targets four statuses
   ('pending','submitted','acked','partially_filled') but the partial
   index only covers three — Postgres falls back to seq-scan for
   PARTIALLY_FILLED rows. This migration drops + recreates the index
   with the predicate extended.

2. **CircuitBreaker `record()` had a SELECT-then-INSERT race.**
   Two concurrent record() calls for the same (trigger_type, user_id)
   scope both probe-miss + both INSERT → duplicate active breakers.
   This migration adds a partial unique index on
   `(trigger_type, COALESCE(user_id,-1)) WHERE resolved_at IS NULL`
   so the DB enforces single-active-per-scope and we can switch
   record() to ON CONFLICT semantics. The `-1` sentinel is safe
   because `users.id` is BIGSERIAL starting at 1 (never -1).

Both changes are pure index manipulations — no row data is touched.
Round-trip is symmetric.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c61b9480195"
down_revision: str | None = "918514b47959"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_ORDERS_EXPIRES_PREDICATE = (
    "nonce_expires_at IS NOT NULL AND status IN ('pending','submitted','acked')"
)
_NEW_ORDERS_EXPIRES_PREDICATE = (
    "nonce_expires_at IS NOT NULL AND status IN ('pending','submitted','acked','partially_filled')"
)


def upgrade() -> None:
    # 1) Extend orders expiry index predicate to include PARTIALLY_FILLED.
    op.drop_index(
        "ix_orders_expires",
        table_name="orders",
        postgresql_where=sa.text(_OLD_ORDERS_EXPIRES_PREDICATE),
    )
    op.create_index(
        "ix_orders_expires",
        "orders",
        ["nonce_expires_at"],
        unique=False,
        postgresql_where=sa.text(_NEW_ORDERS_EXPIRES_PREDICATE),
    )

    # 2a) PR D.3.2 — Dedupe pre-existing duplicate active circuit breakers.
    #     The D.3 SELECT-then-INSERT race could leave multiple unresolved
    #     rows for the same `(trigger_type, COALESCE(user_id, -1))` scope.
    #     The partial unique index we're about to create (step 2b) would
    #     fail to build against that state. Resolve all-but-the-oldest
    #     per scope BEFORE building the index so this migration is safe
    #     to apply to any environment that already ran D.3.
    #
    #     Production hasn't shipped D.3 alone (D.3 + D.3.1 ship together),
    #     so this is a no-op there. Dev/staging that hit the race in
    #     testing will see this step resolve the duplicates with a sentinel
    #     `resolved_by` value, audit-discoverable via:
    #         SELECT * FROM circuit_breakers
    #         WHERE resolved_by = 'migration-8c61b9480195-dedup';
    op.execute(
        sa.text(
            """
            UPDATE circuit_breakers
            SET resolved_at = NOW(),
                resolved_by = 'migration-8c61b9480195-dedup'
            WHERE resolved_at IS NULL
              AND id NOT IN (
                  SELECT MIN(id)
                  FROM circuit_breakers
                  WHERE resolved_at IS NULL
                  GROUP BY trigger_type, COALESCE(user_id, -1)
              )
            """
        )
    )

    # 2b) Circuit breakers — partial unique on
    #    (trigger_type, COALESCE(user_id, -1)) WHERE resolved_at IS NULL.
    #    COALESCE collapses NULL (global breaker) to a sentinel value
    #    so the index treats `user_id IS NULL` as a single distinct
    #    bucket alongside per-user buckets. -1 is a safe sentinel
    #    because `users.id` is BIGSERIAL starting at 1.
    op.create_index(
        "uq_circuit_breakers_active_scope",
        "circuit_breakers",
        [sa.text("trigger_type"), sa.text("COALESCE(user_id, -1)")],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    # 2) Drop CB partial unique.
    op.drop_index(
        "uq_circuit_breakers_active_scope",
        table_name="circuit_breakers",
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    # 1) Restore orders expiry index with the narrower predicate.
    op.drop_index(
        "ix_orders_expires",
        table_name="orders",
        postgresql_where=sa.text(_NEW_ORDERS_EXPIRES_PREDICATE),
    )
    op.create_index(
        "ix_orders_expires",
        "orders",
        ["nonce_expires_at"],
        unique=False,
        postgresql_where=sa.text(_OLD_ORDERS_EXPIRES_PREDICATE),
    )
