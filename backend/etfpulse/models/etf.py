from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class ETFFlow(Base):
    """One row per asset per day.

    `raw_response` is the full SoSoValue API payload (source of truth, kept for
    reprocessing and debugging). `per_etf_flows` is the extracted per-ETF breakdown,
    denormalized for the detector read path so we don't JSON-parse the whole payload
    on every query. Writers must populate both from the same response — do not
    update one without the other.
    """

    __tablename__ = "etf_flows"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    captured_at: Mapped[date] = mapped_column(Date, nullable=False)
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    # USD-denominated ETF flow aggregates. Numeric(22, 2): SoSoValue reports
    # whole-dollar values with 2-decimal precision; 22 digits leaves headroom
    # for trillions (cum_net_inflow grows monotonically and crosses $1T+
    # across BTC+ETH today).
    total_net_flow_usd: Mapped[Decimal] = mapped_column(Numeric(22, 2), nullable=False)
    total_value_traded: Mapped[Decimal | None] = mapped_column(Numeric(22, 2), nullable=True)
    total_net_assets: Mapped[Decimal | None] = mapped_column(Numeric(22, 2), nullable=True)
    cum_net_inflow: Mapped[Decimal | None] = mapped_column(Numeric(22, 2), nullable=True)
    per_etf_flows: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_etf_flows_asset_date", "asset", "captured_at", unique=True),)

    def __repr__(self) -> str:
        return (
            f"<ETFFlow id={self.id} asset={self.asset} "
            f"date={self.captured_at} flow={self.total_net_flow_usd}>"
        )
