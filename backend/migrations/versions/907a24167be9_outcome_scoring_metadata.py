"""outcome scoring metadata — issue #60 scaffolding (PR A)

Adds three nullable columns to `signal_outcomes` so the v2 outcome evaluator
(PR B) can record (a) the per-signal scoring window it used, (b) the rubric
version that produced the row, and (c) the close price at the end of that
window. PR A is schema-only — no code reads these columns yet. The split
mirrors the CLAUDE.md rollback invariant: "column ADD merges in N, code
that reads it merges in N+1."

Existing rows stay NULL on all three columns. NULL is the explicit "legacy
v1 / fixed 72h scoring" sentinel; the v2 reader will treat NULL identically
to `scoring_version='v1'`. Re-evaluation of legacy rows is deferred to
issue #61's one-shot script.

Revision ID: 907a24167be9
Revises: cc33875641da
Create Date: 2026-05-12 17:20:35.261712
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "907a24167be9"
down_revision: str | None = "cc33875641da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("signal_outcomes", sa.Column("window_hours", sa.Integer(), nullable=True))
    op.add_column(
        "signal_outcomes", sa.Column("scoring_version", sa.String(length=8), nullable=True)
    )
    op.add_column(
        "signal_outcomes", sa.Column("price_at_validity_end", sa.Numeric(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("signal_outcomes", "price_at_validity_end")
    op.drop_column("signal_outcomes", "scoring_version")
    op.drop_column("signal_outcomes", "window_hours")
