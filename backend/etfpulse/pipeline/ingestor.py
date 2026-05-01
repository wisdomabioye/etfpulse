"""SoSoValue → DB ingestion jobs.

Each `ingest_*` function is idempotent via Postgres `ON CONFLICT DO NOTHING`
against the unique indexes defined on the models. Callers own the transaction
lifecycle — these functions never commit or rollback, so tests can wrap them
in a rollback-on-exit transaction for isolation.
"""

from __future__ import annotations

from typing import Literal

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.adapters.sosovalue import sosovalue_client
from etfpulse.models import ETFFlow, NewsCategory, NewsItem

log = structlog.get_logger()


async def ingest_etf_flows(
    session: AsyncSession,
    asset: Literal["BTC", "ETH"],
    limit: int = 50,
) -> int:
    """Fetch aggregate ETF flows and insert new rows. Returns # inserted."""
    flows = await sosovalue_client.get_etf_flows(asset, limit=limit)
    if not flows:
        log.info("ingest_etf_flows_empty", asset=asset)
        return 0

    rows = [
        {
            "asset": asset,
            "captured_at": flow.date,
            "total_net_flow_usd": flow.total_net_inflow,
            "total_value_traded": flow.total_value_traded,
            "total_net_assets": flow.total_net_assets,
            "cum_net_inflow": flow.cum_net_inflow,
            # per_etf_flows deferred — see Stage 04.
        }
        for flow in flows
    ]

    stmt = (
        insert(ETFFlow)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["asset", "captured_at"])
        .returning(ETFFlow.id)
    )
    result = await session.execute(stmt)
    inserted = len(result.scalars().all())
    log.info(
        "ingest_etf_flows_done",
        asset=asset,
        fetched=len(flows),
        inserted=inserted,
    )
    return inserted


async def ingest_news(
    session: AsyncSession,
    category: NewsCategory = NewsCategory.INSTITUTION,
    page_size: int = 20,
) -> int:
    """Fetch news for a category and insert new rows. Returns # inserted.

    Defaults to `NewsCategory.INSTITUTION` (=3) since that's what the regime
    monitor and signal explanations rely on. Other categories can be fetched
    by passing `NewsCategory.NEWS`, `ANNOUNCEMENT`, etc.
    """
    # NewsCategory is IntEnum, so it's directly assignable to `int` params.
    # Log the int value (not the enum repr) so downstream JSON consumers stay stable.
    articles = await sosovalue_client.get_news(category=category, page_size=page_size)
    if not articles:
        log.info("ingest_news_empty", category=int(category))
        return 0

    rows = [
        {
            "source_id": article.id,
            "category": article.category,
            "captured_at": article.released_at,
            # Store the raw list so `ix_news_currencies` GIN answers
            # containment queries (e.g. `WHERE currencies @> '[{"symbol":"BTC"}]'`).
            # NULL means "no currencies matched" — use IS NOT NULL to filter.
            "currencies": article.matched_currencies or None,
            "title": article.display_title or None,
            "content_summary": article.stripped_content[:2000] or None,
        }
        for article in articles
    ]

    stmt = (
        insert(NewsItem)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["source_id"])
        .returning(NewsItem.id)
    )
    result = await session.execute(stmt)
    inserted = len(result.scalars().all())
    log.info(
        "ingest_news_done",
        category=int(category),
        fetched=len(articles),
        inserted=inserted,
    )
    return inserted


async def ingest_regime_snapshot(session: AsyncSession) -> None:
    """Populate a RegimeSnapshot row.

    Blocked on the SoSoValue `/currencies/sector-spotlight` endpoint, which
    was un-spikeable when Stage 03 shipped (monthly quota exhausted; see
    docs/API_REFERENCE.md:199 and open_issues.md:126). Wire this up in a
    post-quota micro-stage once a real response shape is captured.
    """
    raise NotImplementedError("see post-quota micro-stage")
