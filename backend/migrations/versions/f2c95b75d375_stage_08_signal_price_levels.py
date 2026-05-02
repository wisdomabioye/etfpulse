"""stage 08 signal price levels

Revision ID: f2c95b75d375
Revises: 28ce6e20dc36
Create Date: 2026-05-02 08:57:21.613611
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c95b75d375"
down_revision: str | None = "28ce6e20dc36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Stage 08-P1 — three new columns mirroring the AI-suggested entry/stop/
    # target prices from `Signal.ai_analysis` JSONB onto dedicated indexable
    # fields. Same pattern as `Signal.confidence` (lives in JSONB AND on a
    # column). Outcome evaluator (`pipeline.track_record`, P2) reads from
    # these columns; never parses JSONB. All nullable: legacy signals built
    # under the v1/v2 prompt have no AI-suggested levels (the v3 prompt is
    # the first that asks for them), AND a "wait" suggestion always leaves
    # them None.
    op.add_column("signals", sa.Column("entry_price", sa.Numeric(), nullable=True))
    op.add_column("signals", sa.Column("stop_price", sa.Numeric(), nullable=True))
    op.add_column("signals", sa.Column("target_price", sa.Numeric(), nullable=True))


def downgrade() -> None:
    # Reverse insertion order — drops are independent (no FKs/indexes on
    # these columns) but mirroring upgrade keeps diffs symmetric.
    op.drop_column("signals", "target_price")
    op.drop_column("signals", "stop_price")
    op.drop_column("signals", "entry_price")
