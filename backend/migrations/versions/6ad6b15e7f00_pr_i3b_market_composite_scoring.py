"""pr_i3b_market_composite_scoring

Revision ID: 6ad6b15e7f00
Revises: e90a892f034a
Create Date: 2026-05-17 22:18:31.448162

Upgrade:
  1. Adds `signal_outcomes.composite_return_pct` (NUMERIC 8,5 nullable)
     for MARKET-asset composite scoring.
  2. Widens `signal_outcomes.scoring_version` from VARCHAR(8) → VARCHAR(16)
     so values like "market-v1" fit alongside the existing "v2" single-
     asset rubric values.
  3. Relaxes `signal_outcomes.price_at_signal` from NOT NULL → NULL. The
     column was always semantically wrong as NOT NULL because the source
     it copies from (`signal.price_at_creation`) IS nullable — MARKET
     signals have no single-asset baseline (PR F.3). The mismatch never
     bit because MARKET signals were filtered out of the candidate query;
     I.3b removes that filter so the constraint must reflect reality.

Downgrade caveats:
  - Narrowing `scoring_version` back to VARCHAR(8) FAILS if any row
    carries a value > 8 chars (e.g. "market-v1"). Operator must first
    DELETE those rows OR rewrite the value (e.g. UPDATE … SET
    scoring_version = NULL WHERE scoring_version LIKE 'market-%').
  - Re-tightening `price_at_signal` to NOT NULL FAILS if any row has
    NULL (i.e. any MARKET-scored outcome). Operator must DELETE those
    rows first (DELETE FROM signal_outcomes WHERE price_at_signal IS NULL).

Both downgrade failure modes are documented here so a future rollback
doesn't crash mid-deploy unexpectedly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6ad6b15e7f00"
down_revision: str | None = "e90a892f034a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "signal_outcomes",
        sa.Column("composite_return_pct", sa.Numeric(precision=8, scale=5), nullable=True),
    )
    op.alter_column(
        "signal_outcomes",
        "scoring_version",
        existing_type=sa.VARCHAR(length=8),
        type_=sa.String(length=16),
        existing_nullable=True,
    )
    # MARKET signals (regime_shift, PR F.3) have no single-asset baseline.
    # The source column `signal.price_at_creation` is already nullable; this
    # change aligns the destination with the source. See module docstring
    # for the latent-mismatch rationale.
    op.alter_column(
        "signal_outcomes",
        "price_at_signal",
        existing_type=sa.Numeric(precision=18, scale=8),
        nullable=True,
    )


def downgrade() -> None:
    # Re-tighten price_at_signal first — narrower-type ALTERs are more
    # likely to fail on bad data; doing it FIRST means downgrades that
    # would fail abort cleanly before any other column is touched.
    op.alter_column(
        "signal_outcomes",
        "price_at_signal",
        existing_type=sa.Numeric(precision=18, scale=8),
        nullable=False,
    )
    op.alter_column(
        "signal_outcomes",
        "scoring_version",
        existing_type=sa.String(length=16),
        type_=sa.VARCHAR(length=8),
        existing_nullable=True,
    )
    op.drop_column("signal_outcomes", "composite_return_pct")
