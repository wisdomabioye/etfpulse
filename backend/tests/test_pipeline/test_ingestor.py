"""Integration tests for pipeline.ingestor — writes against the test DB."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from etfpulse.models import ETFFlow, NewsCategory, NewsItem
from etfpulse.pipeline.ingestor import ingest_etf_flows, ingest_news


async def test_ingest_etf_flows_inserts_unique_rows(db_session):
    inserted = await ingest_etf_flows(db_session, "BTC")
    assert inserted == 21  # fixture has 28 rows spanning 21 unique dates

    count = await db_session.scalar(
        select(func.count()).select_from(ETFFlow).where(ETFFlow.asset == "BTC")
    )
    assert count == 21

    rows = (
        (
            await db_session.execute(
                select(ETFFlow).where(ETFFlow.asset == "BTC").order_by(ETFFlow.captured_at.desc())
            )
        )
        .scalars()
        .all()
    )
    assert rows[0].captured_at.isoformat() == "2026-05-08"
    assert float(rows[0].total_net_flow_usd) == pytest.approx(-145651012.3, rel=1e-9)


async def test_ingest_etf_flows_is_idempotent(db_session):
    first = await ingest_etf_flows(db_session, "BTC")
    second = await ingest_etf_flows(db_session, "BTC")
    assert first == 21
    assert second == 0

    count = await db_session.scalar(select(func.count()).select_from(ETFFlow))
    assert count == 21


async def test_ingest_etf_flows_separates_assets(db_session):
    btc_inserted = await ingest_etf_flows(db_session, "BTC")
    eth_inserted = await ingest_etf_flows(db_session, "ETH")
    assert btc_inserted > 0
    assert eth_inserted > 0

    # ETH + BTC rows coexist
    btc_count = await db_session.scalar(
        select(func.count()).select_from(ETFFlow).where(ETFFlow.asset == "BTC")
    )
    eth_count = await db_session.scalar(
        select(func.count()).select_from(ETFFlow).where(ETFFlow.asset == "ETH")
    )
    assert btc_count == btc_inserted
    assert eth_count == eth_inserted


async def test_ingest_news_inserts_and_strips_html(db_session):
    inserted = await ingest_news(db_session, category=NewsCategory.INSTITUTION)
    assert inserted >= 2

    rows = (await db_session.execute(select(NewsItem))).scalars().all()
    assert len(rows) >= 2

    for row in rows:
        # content_summary must be HTML-stripped
        if row.content_summary:
            assert "<br>" not in row.content_summary
            assert "<img" not in row.content_summary
        assert row.category == 3

    # At least one fixture row has matched_currencies; verify it's stored as a
    # list of {currency_id, symbol, name} dicts (not wrapped in an object).
    with_currencies = [r for r in rows if r.currencies]
    assert with_currencies, "at least one fixture row has matched_currencies"
    first_match = with_currencies[0].currencies[0]
    assert "symbol" in first_match
    assert "currency_id" in first_match


async def test_ingest_news_is_idempotent(db_session):
    first = await ingest_news(db_session, category=NewsCategory.INSTITUTION)
    second = await ingest_news(db_session, category=NewsCategory.INSTITUTION)
    assert first >= 2
    assert second == 0
