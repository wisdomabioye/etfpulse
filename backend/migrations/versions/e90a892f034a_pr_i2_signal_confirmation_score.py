"""pr_i2_signal_confirmation_score — cross-factor voting columns

PR I.2 — empirical confirmation gate for the multi-factor robustness pass.
Adds two nullable columns to `signals` so `build_signal` can record:

  - `confirmation_score` (smallint, 0..3 CHECK) — count of orthogonal
    factors (price, regime, news) whose direction agrees with the AI's
    `suggested_action`. NULL when the AI didn't run or returned "wait"
    (no direction to confirm). News always votes 0 in v1 — the column's
    upper bound of 3 reserves room for v2 when news sentiment lands.

  - `factor_votes` (JSONB) — structured per-factor breakdown:
    `{"price": {"vote": -1|0|+1, "reason": "..."}, "regime": {...},
       "news": {...}}`. Populated alongside `confirmation_score`.

Both NULL by default — preserves rollback compatibility with the prior
app version, which neither writes nor reads these columns. The delivery
gate added in the same PR uses NULL-pass-through (`IS NULL OR >=
threshold`) so existing AI-completed signals don't get cut from delivery
until the backfill script (`scripts/backfill_confirmation.py`) populates
them.

CHECK constraint name is explicit per CLAUDE.md naming-convention rule
(`MetaData.naming_convention` does NOT cover CheckConstraints — see
"Migrations" section for the doubled-name pitfall).

Revision ID: e90a892f034a
Revises: 7e8df33b1932
Create Date: 2026-05-16 21:27:10.109821
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e90a892f034a"
down_revision: str | None = "7e8df33b1932"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "signals",
        sa.Column("confirmation_score", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "signals",
        sa.Column(
            "factor_votes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_signals_confirmation_score_range",
        "signals",
        "confirmation_score IS NULL OR confirmation_score BETWEEN 0 AND 3",
    )
    # Index supports the delivery filter `WHERE confirmation_score >= N`
    # and any future "low-confirmation signals" admin view. Partial on
    # NOT NULL because the table also carries many NULLs (legacy +
    # AI-failed + wait signals); excluding them shrinks the index.
    op.create_index(
        "ix_signals_confirmation",
        "signals",
        ["confirmation_score"],
        postgresql_where=sa.text("confirmation_score IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_signals_confirmation",
        table_name="signals",
        postgresql_where=sa.text("confirmation_score IS NOT NULL"),
    )
    op.drop_constraint("ck_signals_confirmation_score_range", "signals", type_="check")
    op.drop_column("signals", "factor_votes")
    op.drop_column("signals", "confirmation_score")
