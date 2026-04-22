from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class CircuitBreakerTrigger(StrEnum):
    MACRO_EVENT = "macro_event"  # high-impact macro event near — see Stage 09
    MANUAL = "manual"  # admin-initiated halt


class RegimeSnapshot(Base):
    __tablename__ = "regime_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    btc_dominance: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    flow_trend_7d: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    macro_events: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    news_velocity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_interpretation: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_regime_captured", "captured_at"),)

    def __repr__(self) -> str:
        return (
            f"<RegimeSnapshot id={self.id} date={self.captured_at} dominance={self.btc_dominance}>"
        )


class CircuitBreaker(Base):
    __tablename__ = "circuit_breakers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    def __repr__(self) -> str:
        status = "active" if self.resolved_at is None else "resolved"
        return f"<CircuitBreaker id={self.id} type={self.trigger_type} {status}>"
