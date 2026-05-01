"""News-context gatherer — feeds the v2 AI prompt with relevant headlines.

Pure DB read — no adapter calls. Pulls recent `news_items` rows that mention
the asset (via the `currencies` JSONB column's GIN-indexed containment
operator), with a category-only fallback when no asset-tagged news lives
within the window.

Why this lives in `pipeline/` and not in the adapter: the SoSoValue adapter
fetches the raw news firehose (cached 10min); this module *queries the
already-persisted result* to assemble per-signal context. Decoupled so the
AI prompt sees what the rest of the pipeline saw — same DB row, same
`captured_at` truth — instead of re-hitting an external API.

Returns plain serializable dicts (NOT ORM objects) so callers can stuff the
output into `Signal.trigger_data` JSONB without leaking SQLAlchemy state.

Stage 7-P6.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict

import structlog
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.models import NewsCategory, NewsItem

log = structlog.get_logger()


class NewsContextItem(TypedDict):
    """Pinned shape of one item in the `gather_news_context` return list.

    Pinned as a TypedDict so consumers (build_signal storing it as JSONB,
    openrouter._build_messages dumping it into the prompt, the future
    frontend regime/signal-detail surfaces) all see one contract.

    `title` is nullable because SoSoValue news items frequently arrive with
    `title=null` (tweet-style content where only `content_summary` carries
    the headline). `summary` is nullable when the persisted
    `content_summary` is empty after HTML strip — JSONB-friendly null
    rather than empty string.

    NOTE: an `author` field is intentionally absent — the persisted
    `NewsItem` model has no author column (only the adapter DTO does).
    Add a column + ingest path before adding it here.
    """

    title: str | None
    category: int
    published_iso: str
    summary: str | None


# Default lookback window. 24h matches the news-velocity window in
# regime_monitor — same scope of "recent" across the AI surface area.
_DEFAULT_HOURS = 24

# Default cap on items returned. The AI prompt fits comfortably with 5
# headlines + their summaries truncated to ~200 chars each.
_DEFAULT_MAX_ITEMS = 5

# Cap on the per-item content_summary length passed to the AI. Keeps the
# total prompt under ~2k tokens even when all 5 items have full summaries.
_SUMMARY_TRUNCATE = 200


async def gather_news_context(
    session: AsyncSession,
    asset: str,
    hours: int = _DEFAULT_HOURS,
    max_items: int = _DEFAULT_MAX_ITEMS,
) -> list[NewsContextItem]:
    """Return recent, asset-relevant news items as `NewsContextItem` dicts.

    Strategy:
      1. Filter `news_items` to rows captured within `hours` of now.
      2. Prefer rows where `currencies @> [{"symbol": <asset>}]` (uses
         the `ix_news_currencies` GIN index, verified at
         `models/news.py:48`).
      3. If that returns nothing, fall back to category-only — recent
         INSTITUTION + ANNOUNCEMENT items regardless of asset tag. Better
         to give the AI macro/regulator context than nothing.
      4. Order newest-first; cap at `max_items`.

    See `NewsContextItem` for the per-item field contract.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)

    # Asset-matched query (preferred path).
    asset_match_value: list[dict[str, str]] = [{"symbol": asset}]
    asset_stmt = (
        select(NewsItem)
        .where(NewsItem.captured_at >= cutoff)
        .where(NewsItem.currencies.contains(asset_match_value))
        .order_by(desc(NewsItem.captured_at))
        .limit(max_items)
    )
    rows = (await session.execute(asset_stmt)).scalars().all()

    if not rows:
        # Fallback: category-scoped news (no asset filter). The two
        # categories ingested every cycle. We still cap at `max_items`.
        log.info(
            "news_context_asset_empty_falling_back",
            asset=asset,
            hours=hours,
        )
        fallback_stmt = (
            select(NewsItem)
            .where(NewsItem.captured_at >= cutoff)
            .where(
                NewsItem.category.in_(
                    [
                        int(NewsCategory.INSTITUTION),
                        int(NewsCategory.ANNOUNCEMENT),
                    ]
                )
            )
            .order_by(desc(NewsItem.captured_at))
            .limit(max_items)
        )
        rows = (await session.execute(fallback_stmt)).scalars().all()

    items = [_to_dict(row) for row in rows]
    log.info(
        "news_context_gathered",
        asset=asset,
        hours=hours,
        count=len(items),
    )
    return items


def _to_dict(item: NewsItem) -> NewsContextItem:
    """Project a NewsItem ORM row to a `NewsContextItem`.

    Truncates `content_summary` so the prompt stays bounded; keeps the full
    summary on disk in case the dashboard ever wants to render it.
    """
    summary = item.content_summary or ""
    if len(summary) > _SUMMARY_TRUNCATE:
        summary = summary[: _SUMMARY_TRUNCATE - 1].rstrip() + "…"

    return NewsContextItem(
        title=item.title,
        category=item.category,
        published_iso=item.captured_at.astimezone(UTC).isoformat(),
        summary=summary or None,
    )
