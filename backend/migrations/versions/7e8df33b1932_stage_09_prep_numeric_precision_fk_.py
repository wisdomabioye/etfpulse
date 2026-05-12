"""stage 09 prep — numeric precision + fk ondelete + status checks + paper trade

Bundles issues #64, #69, #70, #71 into one atomic schema change. Sets the
foundation for Stage 09 SoDEX execution by:

  - #64: adds `paper_trade` boolean to `orders` + `positions`, and `raw_request`
    JSONB to `orders` (for outbound payload audit alongside `raw_response`).
  - #69: tightens Numeric precision/scale across all money/quantity columns
    in Signal/SignalOutcome/Order/Position, plus the previously-unconstrained
    Numeric columns in ETFFlow (USD aggregates) and RegimeSnapshot (extended
    in review pass). Prices → Numeric(18, 8); sizes → Numeric(28, 18);
    fractions → Numeric(8, 4); USD aggregates → Numeric(22, 2). Pre-Stage-09
    rows all comfortably fit; ALTER will raise if any row overflows, which
    is the desired behaviour.
  - #70: declares FK ondelete policies (CASCADE on owned children; SET NULL on
    optional refs in execution tables). signal_deliveries uses CASCADE on its
    three recipient FKs because `ck_delivery_target` requires user_id+channel_id
    OR group_id to be NOT NULL — SET NULL would violate it.
  - #71: adds CHECK constraints pinning status / trigger_type enum values to
    the StrEnum's declared set.

Revision ID: 7e8df33b1932
Revises: 907a24167be9
Create Date: 2026-05-12 23:53:14.613174
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7e8df33b1932"
down_revision: str | None = "907a24167be9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------- Numeric precision tables ----------------------------------------
# (table, column, new_type, old_type-on-downgrade)
PRICE = sa.Numeric(18, 8)
SIZE = sa.Numeric(28, 18)
FRACTION = sa.Numeric(8, 4)
USD_AGG = sa.Numeric(22, 2)  # whole-dollar ETF flow aggregates (room for trillions)
PLAIN = sa.Numeric()

_NUMERIC_CHANGES: list[tuple[str, str, sa.types.TypeEngine]] = [
    # etf_flows — USD aggregates (review-pass extension)
    ("etf_flows", "total_net_flow_usd", USD_AGG),
    ("etf_flows", "total_value_traded", USD_AGG),
    ("etf_flows", "total_net_assets", USD_AGG),
    ("etf_flows", "cum_net_inflow", USD_AGG),
    # regime_snapshots — fractional values (review-pass extension)
    ("regime_snapshots", "btc_dominance", FRACTION),
    ("regime_snapshots", "flow_trend_7d", FRACTION),
    # signals
    ("signals", "price_at_creation", PRICE),
    ("signals", "entry_price", PRICE),
    ("signals", "stop_price", PRICE),
    ("signals", "target_price", PRICE),
    # signal_outcomes
    ("signal_outcomes", "entry_price", PRICE),
    ("signal_outcomes", "stop_price", PRICE),
    ("signal_outcomes", "target_price", PRICE),
    ("signal_outcomes", "price_at_signal", PRICE),
    ("signal_outcomes", "price_after_24h", PRICE),
    ("signal_outcomes", "price_after_72h", PRICE),
    ("signal_outcomes", "price_at_validity_end", PRICE),
    ("signal_outcomes", "max_favorable", FRACTION),
    ("signal_outcomes", "max_adverse", FRACTION),
    # orders
    ("orders", "requested_size", SIZE),
    ("orders", "filled_size", SIZE),
    ("orders", "requested_price", PRICE),
    ("orders", "filled_price", PRICE),
    ("orders", "filled_value", PRICE),
    ("orders", "fees", PRICE),
    # positions
    ("positions", "size", SIZE),
    ("positions", "entry_price", PRICE),
    ("positions", "stop_loss", PRICE),
    ("positions", "take_profit", PRICE),
    ("positions", "close_price", PRICE),
    ("positions", "realized_pnl", PRICE),
]


# ---------- CHECK constraints (#71) -----------------------------------------
# (table, name, sql)
_STATUS_CHECKS: list[tuple[str, str, str]] = [
    ("signals", "ck_signals_status_enum", "status IN ('pending','alerted','expired')"),
    (
        "signal_deliveries",
        "ck_signal_deliveries_status_enum",
        "status IN ('pending','delivered','failed','skipped')",
    ),
    (
        "orders",
        "ck_orders_status_enum",
        "status IN ('pending','partially_filled','filled','cancelled','rejected')",
    ),
    ("positions", "ck_positions_status_enum", "status IN ('open','closed','cancelled')"),
    (
        "circuit_breakers",
        "ck_circuit_breakers_trigger_type_enum",
        "trigger_type IN ('macro_event','manual')",
    ),
]


def upgrade() -> None:
    # ---- #64: paper_trade + raw_request -----------------------------------
    op.add_column(
        "orders",
        sa.Column("paper_trade", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column("raw_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("paper_trade", sa.Boolean(), server_default="false", nullable=False),
    )

    # ---- #69: Numeric precision -------------------------------------------
    for table, column, new_type in _NUMERIC_CHANGES:
        # Postgres ALTER COLUMN TYPE preserves nullability; existing_nullable
        # would only be needed for Oracle. Omitting avoids stating it wrong.
        op.alter_column(table, column, type_=new_type)

    # ---- #70: FK ondelete policies ----------------------------------------
    # notification_channels.user_id -> CASCADE
    op.drop_constraint(
        op.f("fk_notification_channels_user_id_users"),
        "notification_channels",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_notification_channels_user_id_users"),
        "notification_channels",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # signal_outcomes.signal_id -> CASCADE
    op.drop_constraint(
        op.f("fk_signal_outcomes_signal_id_signals"), "signal_outcomes", type_="foreignkey"
    )
    op.create_foreign_key(
        op.f("fk_signal_outcomes_signal_id_signals"),
        "signal_outcomes",
        "signals",
        ["signal_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # signal_deliveries: signal_id + user_id + group_id + channel_id -> CASCADE
    op.drop_constraint(
        op.f("fk_signal_deliveries_signal_id_signals"),
        "signal_deliveries",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_signal_deliveries_user_id_users"),
        "signal_deliveries",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_signal_deliveries_group_id_telegram_groups"),
        "signal_deliveries",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_signal_deliveries_channel_id_notification_channels"),
        "signal_deliveries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_signal_deliveries_signal_id_signals"),
        "signal_deliveries",
        "signals",
        ["signal_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_signal_deliveries_user_id_users"),
        "signal_deliveries",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_signal_deliveries_group_id_telegram_groups"),
        "signal_deliveries",
        "telegram_groups",
        ["group_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_signal_deliveries_channel_id_notification_channels"),
        "signal_deliveries",
        "notification_channels",
        ["channel_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # orders.user_id + signal_id -> SET NULL
    op.drop_constraint(op.f("fk_orders_user_id_users"), "orders", type_="foreignkey")
    op.drop_constraint(op.f("fk_orders_signal_id_signals"), "orders", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_orders_user_id_users"),
        "orders",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_orders_signal_id_signals"),
        "orders",
        "signals",
        ["signal_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # positions.user_id + signal_id + order_id -> SET NULL
    op.drop_constraint(op.f("fk_positions_user_id_users"), "positions", type_="foreignkey")
    op.drop_constraint(op.f("fk_positions_signal_id_signals"), "positions", type_="foreignkey")
    op.drop_constraint(op.f("fk_positions_order_id_orders"), "positions", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_positions_user_id_users"),
        "positions",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_positions_signal_id_signals"),
        "positions",
        "signals",
        ["signal_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_positions_order_id_orders"),
        "positions",
        "orders",
        ["order_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---- #71: status / trigger_type CHECKs --------------------------------
    for table, name, sql in _STATUS_CHECKS:
        op.create_check_constraint(name, table, sql)


def downgrade() -> None:
    # ---- #71 reverse -------------------------------------------------------
    for table, name, _sql in _STATUS_CHECKS:
        op.drop_constraint(name, table, type_="check")

    # ---- #70 reverse — drop ondelete (revert to no policy) ----------------
    op.drop_constraint(op.f("fk_positions_order_id_orders"), "positions", type_="foreignkey")
    op.drop_constraint(op.f("fk_positions_signal_id_signals"), "positions", type_="foreignkey")
    op.drop_constraint(op.f("fk_positions_user_id_users"), "positions", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_positions_order_id_orders"), "positions", "orders", ["order_id"], ["id"]
    )
    op.create_foreign_key(
        op.f("fk_positions_signal_id_signals"), "positions", "signals", ["signal_id"], ["id"]
    )
    op.create_foreign_key(
        op.f("fk_positions_user_id_users"), "positions", "users", ["user_id"], ["id"]
    )

    op.drop_constraint(op.f("fk_orders_signal_id_signals"), "orders", type_="foreignkey")
    op.drop_constraint(op.f("fk_orders_user_id_users"), "orders", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_orders_signal_id_signals"), "orders", "signals", ["signal_id"], ["id"]
    )
    op.create_foreign_key(op.f("fk_orders_user_id_users"), "orders", "users", ["user_id"], ["id"])

    op.drop_constraint(
        op.f("fk_signal_deliveries_channel_id_notification_channels"),
        "signal_deliveries",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_signal_deliveries_group_id_telegram_groups"),
        "signal_deliveries",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_signal_deliveries_user_id_users"), "signal_deliveries", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_signal_deliveries_signal_id_signals"), "signal_deliveries", type_="foreignkey"
    )
    op.create_foreign_key(
        op.f("fk_signal_deliveries_channel_id_notification_channels"),
        "signal_deliveries",
        "notification_channels",
        ["channel_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_signal_deliveries_group_id_telegram_groups"),
        "signal_deliveries",
        "telegram_groups",
        ["group_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_signal_deliveries_user_id_users"),
        "signal_deliveries",
        "users",
        ["user_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_signal_deliveries_signal_id_signals"),
        "signal_deliveries",
        "signals",
        ["signal_id"],
        ["id"],
    )

    op.drop_constraint(
        op.f("fk_signal_outcomes_signal_id_signals"), "signal_outcomes", type_="foreignkey"
    )
    op.create_foreign_key(
        op.f("fk_signal_outcomes_signal_id_signals"),
        "signal_outcomes",
        "signals",
        ["signal_id"],
        ["id"],
    )

    op.drop_constraint(
        op.f("fk_notification_channels_user_id_users"),
        "notification_channels",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_notification_channels_user_id_users"),
        "notification_channels",
        "users",
        ["user_id"],
        ["id"],
    )

    # ---- #69 reverse — back to unconstrained Numeric ----------------------
    for table, column, _new_type in reversed(_NUMERIC_CHANGES):
        op.alter_column(table, column, type_=PLAIN, existing_nullable=True)

    # ---- #64 reverse -------------------------------------------------------
    op.drop_column("positions", "paper_trade")
    op.drop_column("orders", "raw_request")
    op.drop_column("orders", "paper_trade")
