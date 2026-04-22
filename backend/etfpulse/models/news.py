from datetime import datetime
from enum import IntEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from etfpulse.models.base import Base


class NewsCategory(IntEnum):
    """SoSoValue news category codes (from docs/API_REFERENCE.md:145).

    Stored on disk as int to round-trip the SoSoValue API exactly. Callers that
    need a human label should use `NewsCategory(news.category).name`.
    Unknown future codes are tolerated at DB level — the column accepts any int
    and `NewsCategory(n)` will raise for unmapped values, letting callers decide.
    """

    NEWS = 1
    RESEARCH = 2
    INSTITUTION = 3
    KOL = 4
    ANNOUNCEMENT = 7
    CRYPTO_STOCK = 13


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    category: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # List of {currency_id, symbol, name} dicts matched by SoSoValue's NLP.
    # Stored as a JSONB array (not wrapped in an object) so `ix_news_currencies`
    # supports containment queries like `currencies @> '[{"symbol":"BTC"}]'`.
    currencies: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_news_category_date", "category", "captured_at"),
        Index("ix_news_currencies", "currencies", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        display = self.title or (self.content_summary or "")[:50]
        return f"<NewsItem id={self.id} cat={self.category} title={display}>"
