"""news_context.gather_news_context — DB integration tests.

Covers:
  - Asset-tagged news returned via JSONB containment (uses
    `ix_news_currencies` GIN index)
  - Fallback to category-only when no asset-tagged news exists in window
  - Empty result when both asset and fallback queries return nothing
  - `max_items` truncation
  - Stale items (outside the window) filtered out
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from etfpulse.models import NewsCategory, NewsItem
from etfpulse.pipeline.news_context import gather_news_context


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


def _btc_currency() -> list[dict[str, str]]:
    return [
        {"currency_id": "1673723677362319866", "symbol": "BTC", "name": "Bitcoin"},
    ]


def _eth_currency() -> list[dict[str, str]]:
    return [
        {"currency_id": "1673723677362319867", "symbol": "ETH", "name": "Ethereum"},
    ]


class TestAssetMatch:
    async def test_returns_btc_tagged_rows(self, db_session, now):
        db_session.add(
            NewsItem(
                source_id="news-asset-btc-1",
                category=int(NewsCategory.INSTITUTION),
                captured_at=now - timedelta(hours=2),
                currencies=_btc_currency(),
                title="BlackRock files spot BTC ETF amendment",
                content_summary="The amendment touches creation/redemption mechanics.",
            )
        )
        db_session.add(
            NewsItem(
                source_id="news-asset-eth-1",
                category=int(NewsCategory.INSTITUTION),
                captured_at=now - timedelta(hours=1),
                currencies=_eth_currency(),
                title="ETH staking ETF news",
                content_summary="ETH-only article that should not appear.",
            )
        )
        await db_session.flush()

        items = await gather_news_context(db_session, asset="BTC")

        titles = [i["title"] for i in items]
        assert "BlackRock files spot BTC ETF amendment" in titles
        assert "ETH staking ETF news" not in titles

    async def test_orders_newest_first(self, db_session, now):
        for i in range(3):
            db_session.add(
                NewsItem(
                    source_id=f"news-order-{i}",
                    category=int(NewsCategory.INSTITUTION),
                    captured_at=now - timedelta(hours=i + 1),
                    currencies=_btc_currency(),
                    title=f"BTC news {i}",
                    content_summary="…",
                )
            )
        await db_session.flush()

        items = await gather_news_context(db_session, asset="BTC")
        # i=0 is newest (1 hour ago); i=2 is oldest (3 hours ago).
        assert [i["title"] for i in items] == [
            "BTC news 0",
            "BTC news 1",
            "BTC news 2",
        ]

    async def test_max_items_truncates(self, db_session, now):
        for i in range(8):
            db_session.add(
                NewsItem(
                    source_id=f"news-max-{i}",
                    category=int(NewsCategory.INSTITUTION),
                    captured_at=now - timedelta(minutes=i + 1),
                    currencies=_btc_currency(),
                    title=f"BTC item {i}",
                )
            )
        await db_session.flush()

        items = await gather_news_context(db_session, asset="BTC", max_items=3)
        assert len(items) == 3

    async def test_outside_window_excluded(self, db_session, now):
        db_session.add(
            NewsItem(
                source_id="news-stale",
                category=int(NewsCategory.INSTITUTION),
                captured_at=now - timedelta(hours=48),  # outside default 24h
                currencies=_btc_currency(),
                title="STALE: 48h-old article",
            )
        )
        await db_session.flush()

        items = await gather_news_context(db_session, asset="BTC")
        assert items == []

    async def test_asset_match_does_not_fall_back_when_results_exist(self, db_session, now):
        """Coverage gap closer — proves the asset filter PRECEDES the fallback.

        Seed BOTH (a) one BTC-tagged row AND (b) one untagged-but-eligible
        category-only row. The asset path should win and the untagged row
        should NEVER appear, even though the fallback would have included it.
        """
        db_session.add(
            NewsItem(
                source_id="news-asset-precedence-tagged",
                category=int(NewsCategory.INSTITUTION),
                captured_at=now - timedelta(hours=1),
                currencies=_btc_currency(),
                title="ASSET-TAGGED",
            )
        )
        db_session.add(
            NewsItem(
                source_id="news-asset-precedence-untagged",
                category=int(NewsCategory.INSTITUTION),
                captured_at=now,  # newer than the tagged one
                currencies=None,
                title="UNTAGGED-FALLBACK-ELIGIBLE",
            )
        )
        await db_session.flush()

        items = await gather_news_context(db_session, asset="BTC")
        titles = [i["title"] for i in items]
        assert "ASSET-TAGGED" in titles
        # The untagged row must NOT leak through — fallback only fires
        # when the asset-tagged result set is empty.
        assert "UNTAGGED-FALLBACK-ELIGIBLE" not in titles

    async def test_summary_is_truncated_for_prompt_size(self, db_session, now):
        long_text = "x" * 500
        db_session.add(
            NewsItem(
                source_id="news-long",
                category=int(NewsCategory.INSTITUTION),
                captured_at=now,
                currencies=_btc_currency(),
                title="long content",
                content_summary=long_text,
            )
        )
        await db_session.flush()

        items = await gather_news_context(db_session, asset="BTC")
        assert len(items) == 1
        # Cap is 200 chars including the trailing ellipsis. We don't pin the
        # exact length here — just that it's bounded.
        assert items[0]["summary"] is not None
        assert len(items[0]["summary"]) <= 200


class TestFallback:
    async def test_falls_back_to_category_when_no_asset_tag(self, db_session, now):
        """No BTC-tagged news in window → returns recent INSTITUTION /
        ANNOUNCEMENT items regardless of asset."""
        db_session.add(
            NewsItem(
                source_id="news-fallback-1",
                category=int(NewsCategory.INSTITUTION),
                captured_at=now - timedelta(hours=2),
                currencies=None,  # no asset tag at all
                title="Generic institutional news",
            )
        )
        db_session.add(
            NewsItem(
                source_id="news-fallback-2",
                category=int(NewsCategory.ANNOUNCEMENT),
                captured_at=now - timedelta(hours=1),
                currencies=None,
                title="Regulator announcement",
            )
        )
        await db_session.flush()

        items = await gather_news_context(db_session, asset="BTC")
        titles = sorted(i["title"] for i in items if i["title"])
        assert titles == ["Generic institutional news", "Regulator announcement"]

    async def test_fallback_skips_off_categories(self, db_session, now):
        """Fallback only includes INSTITUTION + ANNOUNCEMENT — KOL etc.
        shouldn't be promoted into context."""
        db_session.add(
            NewsItem(
                source_id="news-kol",
                category=int(NewsCategory.KOL),
                captured_at=now - timedelta(hours=1),
                currencies=None,
                title="KOL hot take",
            )
        )
        await db_session.flush()

        items = await gather_news_context(db_session, asset="BTC")
        assert items == []


class TestEmpty:
    async def test_empty_db_returns_empty_list(self, db_session):
        items = await gather_news_context(db_session, asset="BTC")
        assert items == []
