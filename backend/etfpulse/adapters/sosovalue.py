"""SoSoValue API adapter.

Covers the three endpoints Wave 1 needs: ETF summary history, news feed, and
macro events. `/currencies/sector-spotlight` is intentionally absent — it was
not spikeable due to monthly-quota exhaustion (see docs/API_REFERENCE.md:199
and pipeline/ingestor.py:ingest_regime_snapshot).

Auth: `x-soso-api-key` header (NOT Bearer token).
Rate limits: 20 req/min and 100k req/month; both return HTTP 429 with the same
error code (402901) but different `message` text — we distinguish by message.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import structlog
from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict, field_validator

from etfpulse.config import settings

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BTC_CURRENCY_ID = "1673723677362319866"
ETH_CURRENCY_ID = "1673723677362319867"

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"

_REQUEST_TIMEOUT = 30.0
_RATE_LIMIT_BACKOFF_SECONDS = 3.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SoSoValueError(Exception):
    """Base class for adapter errors."""


class SoSoValueRateLimitError(SoSoValueError):
    """Per-minute rate limit tripped. Caller may retry after a short backoff."""


class SoSoValueMonthlyQuotaError(SoSoValueError):
    """Monthly quota (100k calls) exhausted. Retrying is pointless until reset."""


# ---------------------------------------------------------------------------
# Pydantic DTOs
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class ETFFlowPoint(BaseModel):
    """One day of aggregate ETF flow (source: GET /etfs/summary-history)."""

    model_config = ConfigDict(frozen=True)

    date: date
    total_net_inflow: Decimal
    total_value_traded: Decimal
    total_net_assets: Decimal
    cum_net_inflow: Decimal


class NewsArticle(BaseModel):
    """Single news item (source: GET /news).

    Gotchas from the spike:
    - `title` is often null (tweets have content only).
    - `content` is HTML with <br> and <img> tags.
    - `release_time` is a millisecond timestamp stored as a string.
    - Engagement counts are strings, not ints (we don't parse them).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str | None = None
    content: str = ""
    category: int
    release_time: str
    author: str | None = None
    matched_currencies: list[dict[str, Any]] = []
    tags: list[str] = []
    source_link: str | None = None
    original_link: str | None = None

    @field_validator("matched_currencies", "tags", mode="before")
    @classmethod
    def _none_to_empty_list(cls, v: Any) -> Any:
        """Fixture data has `null` where the API would put `[]`. Coerce."""
        return v if v is not None else []

    @property
    def released_at(self) -> datetime:
        return datetime.fromtimestamp(int(self.release_time) / 1000, tz=UTC)

    @property
    def display_title(self) -> str:
        """Best-effort human-readable title: real title if set, else stripped content."""
        if self.title:
            return self.title
        return self.stripped_content[:100].strip()

    @property
    def stripped_content(self) -> str:
        """Content with HTML tags removed and whitespace collapsed."""
        no_tags = _HTML_TAG_RE.sub(" ", self.content)
        return _WHITESPACE_RE.sub(" ", no_tags).strip()


class MacroEvent(BaseModel):
    """Upcoming macro event (source: GET /macro/events)."""

    model_config = ConfigDict(frozen=True)

    date: date
    events: list[str]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SoSoValueClient:
    """Thin async wrapper over the SoSoValue REST API.

    One client instance holds TTL caches; multiple instances do not share cache
    state. Tests should instantiate fresh clients (or clear caches) rather than
    rely on the module singleton below.
    """

    def __init__(self) -> None:
        self.base_url = settings.sosovalue_base_url.rstrip("/")
        self._flows_cache: TTLCache[str, list[ETFFlowPoint]] = TTLCache(maxsize=50, ttl=6 * 3600)
        self._news_cache: TTLCache[str, list[NewsArticle]] = TTLCache(maxsize=50, ttl=600)
        self._macro_cache: TTLCache[str, list[MacroEvent]] = TTLCache(maxsize=10, ttl=86400)

    @property
    def use_fixtures(self) -> bool:
        """Read live from settings so tests can flip the flag after init."""
        return settings.sosovalue_use_fixtures

    @property
    def api_key(self) -> str:
        return settings.sosovalue_api_key

    # --- HTTP ---------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Make one authenticated request. Retries once on per-minute 429."""
        url = f"{self.base_url}{path}"
        headers = {"x-soso-api-key": self.api_key, "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            try:
                response = await client.request(method, url, headers=headers, **kwargs)
            except httpx.RequestError as exc:
                log.error("sosovalue_request_failed", path=path, error=str(exc))
                raise SoSoValueError(f"network error: {exc}") from exc

            if response.status_code == 429:
                self._classify_and_raise_429(response, path)
                # Per-minute rate limit — wait briefly then retry once.
                log.warning(
                    "sosovalue_rate_limited", path=path, backoff=_RATE_LIMIT_BACKOFF_SECONDS
                )
                await asyncio.sleep(_RATE_LIMIT_BACKOFF_SECONDS)
                try:
                    response = await client.request(method, url, headers=headers, **kwargs)
                except httpx.RequestError as exc:
                    raise SoSoValueError(f"network error on retry: {exc}") from exc
                if response.status_code == 429:
                    self._classify_and_raise_429(response, path)
                    raise SoSoValueRateLimitError(f"rate limit still tripped after retry on {path}")

            if response.status_code >= 400:
                log.error(
                    "sosovalue_http_error",
                    path=path,
                    status=response.status_code,
                    body=response.text[:500],
                )
                raise SoSoValueError(
                    f"HTTP {response.status_code} on {path}: {response.text[:200]}"
                )

            try:
                return cast(dict[str, Any], response.json())
            except ValueError as exc:
                log.error(
                    "sosovalue_invalid_json",
                    path=path,
                    status=response.status_code,
                    body=response.text[:500],
                )
                raise SoSoValueError(f"invalid JSON from {path}: {exc}") from exc

    @staticmethod
    def _classify_and_raise_429(response: httpx.Response, path: str) -> None:
        """Raise `SoSoValueMonthlyQuotaError` if the 429 is monthly-quota.

        Rate-limit 429s return without raising so the caller can retry.
        """
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message", ""))
        if "Monthly quota" in message:
            log.error("sosovalue_monthly_quota_exceeded", path=path, message=message)
            raise SoSoValueMonthlyQuotaError(message)

    # --- Fixtures -----------------------------------------------------------

    @staticmethod
    def _load_fixture(name: str) -> dict[str, Any]:
        """Read backend/fixtures/{name}.json. Returns {} if the file is missing."""
        fixture_path = FIXTURES_DIR / f"{name}.json"
        if not fixture_path.exists():
            log.warning("sosovalue_fixture_missing", name=name, path=str(fixture_path))
            return {}
        return cast(dict[str, Any], json.loads(fixture_path.read_text()))

    # --- Public methods -----------------------------------------------------

    async def get_etf_flows(
        self,
        asset: Literal["BTC", "ETH"],
        limit: int = 50,
    ) -> list[ETFFlowPoint]:
        """Aggregate daily ETF flows for BTC or ETH.

        The API returns multiple rows per date (rolling-window aggregates). We
        keep the first row per date — the spike confirmed this is the
        single-day value (smallest `total_value_traded`).
        """
        cache_key = f"flows_{asset}_{limit}"
        if cache_key in self._flows_cache:
            return self._flows_cache[cache_key]

        if self.use_fixtures:
            raw = self._load_fixture(f"sosovalue_etf_flows_{asset.lower()}")
        else:
            raw = await self._request(
                "GET",
                "/etfs/summary-history",
                params={"symbol": asset, "country_code": "US", "limit": limit},
            )

        seen: set[str] = set()
        flows: list[ETFFlowPoint] = []
        for item in raw.get("data", []) or []:
            date_str = item["date"]
            if date_str in seen:
                continue
            seen.add(date_str)
            flows.append(ETFFlowPoint(**item))

        self._flows_cache[cache_key] = flows
        log.info("sosovalue_etf_flows_fetched", asset=asset, count=len(flows))
        return flows

    async def get_news(
        self,
        category: int | None = None,
        currency_id: str | None = None,
        language: str = "en",
        page: int = 1,
        page_size: int = 20,
        fixture_name: str = "sosovalue_news_institution",
    ) -> list[NewsArticle]:
        """News feed. Categories: 1=News, 2=Research, 3=Institution, 4=KOL,
        7=Announcement, 13=CryptoStock. `fixture_name` only matters when
        `use_fixtures=True`.
        """
        cache_key = f"news_{category}_{currency_id}_{page}_{page_size}"
        if cache_key in self._news_cache:
            return self._news_cache[cache_key]

        if self.use_fixtures:
            raw = self._load_fixture(fixture_name)
        else:
            params: dict[str, Any] = {
                "language": language,
                "page": page,
                "page_size": page_size,
            }
            if category is not None:
                params["category"] = category
            if currency_id:
                params["currency_id"] = currency_id
            raw = await self._request("GET", "/news", params=params)

        # News response nests under data.list (paginated envelope).
        data_block = raw.get("data") or {}
        items = data_block.get("list", []) if isinstance(data_block, dict) else []

        articles = [NewsArticle(**item) for item in items]
        self._news_cache[cache_key] = articles
        log.info("sosovalue_news_fetched", count=len(articles), category=category, page=page)
        return articles

    async def get_macro_events(
        self,
        page: int = 1,
        page_size: int = 10,
    ) -> list[MacroEvent]:
        """Upcoming macro economic events (FOMC, CPI, NFP, etc)."""
        cache_key = f"macro_{page}_{page_size}"
        if cache_key in self._macro_cache:
            return self._macro_cache[cache_key]

        if self.use_fixtures:
            raw = self._load_fixture("sosovalue_macro_events")
        else:
            raw = await self._request(
                "GET",
                "/macro/events",
                params={"page": page, "page_size": page_size},
            )

        events = [MacroEvent(**item) for item in raw.get("data", []) or []]
        self._macro_cache[cache_key] = events
        log.info("sosovalue_macro_events_fetched", count=len(events))
        return events


# Module-level singleton. Tests that need isolation should instantiate
# SoSoValueClient() directly rather than using this.
sosovalue_client = SoSoValueClient()
