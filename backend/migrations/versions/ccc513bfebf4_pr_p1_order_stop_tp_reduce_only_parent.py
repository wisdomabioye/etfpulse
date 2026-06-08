"""pr_p1_order_stop_tp_reduce_only_parent

Revision ID: ccc513bfebf4
Revises: 8c61b9480195
Create Date: 2026-06-08 12:17:33.579585
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ccc513bfebf4"
down_revision: str | None = "8c61b9480195"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders", sa.Column("stop_price", sa.Numeric(precision=18, scale=8), nullable=True)
    )
    op.add_column("orders", sa.Column("stop_type", sa.String(length=20), nullable=True))
    op.add_column(
        "orders", sa.Column("reduce_only", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column("orders", sa.Column("parent_order_id", sa.BigInteger(), nullable=True))
    op.create_index(
        "ix_orders_parent",
        "orders",
        ["parent_order_id"],
        unique=False,
        postgresql_where=sa.text("parent_order_id IS NOT NULL"),
    )
    op.create_foreign_key(
        op.f("fk_orders_parent_order_id_orders"),
        "orders",
        "orders",
        ["parent_order_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # CHECK constraints — autogen doesn't pick these up from the model's
    # __table_args__ on column-add migrations, so they go in manually.
    # Names match the model's CheckConstraint(..., name=...) literals.
    op.create_check_constraint(
        "ck_orders_stop_price_positive",
        "orders",
        "stop_price IS NULL OR stop_price > 0",
    )
    op.create_check_constraint(
        "ck_orders_stop_type_enum",
        "orders",
        "stop_type IS NULL OR stop_type IN ('stop_loss','take_profit')",
    )
    op.create_check_constraint(
        "ck_orders_stop_price_type_consistency",
        "orders",
        "(stop_price IS NULL) = (stop_type IS NULL)",
    )
    op.create_check_constraint(
        "ck_orders_parent_not_self",
        "orders",
        "parent_order_id IS NULL OR parent_order_id <> id",
    )


def downgrade() -> None:
    op.drop_constraint("ck_orders_parent_not_self", "orders", type_="check")
    op.drop_constraint("ck_orders_stop_price_type_consistency", "orders", type_="check")
    op.drop_constraint("ck_orders_stop_type_enum", "orders", type_="check")
    op.drop_constraint("ck_orders_stop_price_positive", "orders", type_="check")
    op.drop_constraint(op.f("fk_orders_parent_order_id_orders"), "orders", type_="foreignkey")
    op.drop_index(
        "ix_orders_parent",
        table_name="orders",
        postgresql_where=sa.text("parent_order_id IS NOT NULL"),
    )
    op.drop_column("orders", "parent_order_id")
    op.drop_column("orders", "reduce_only")
    op.drop_column("orders", "stop_type")
    op.drop_column("orders", "stop_price")
