"""SoSoValue API adapter.

Covers ETF summary history, news feed, macro events, market snapshot,
daily klines, and sector-spotlight. All endpoints share the
`{code, message, data, details}` envelope — adapter unwraps `data` and
parses into Pydantic DTOs.

Auth: `x-soso-api-key` header (NOT Bearer token). Missing/invalid key →
HTTP 401 with `{"code":400101, "message":"API Key is invalid or does not exist"}`.

Rate limits: per-minute + monthly-quota; both return HTTP 429 with the same
error code (402901), distinguished by `message` substring. Documented
per-minute floor is "20 req/min" but a live probe (35 parallel calls in
1.88s) returned 200 across the board on the upgraded tier — actual
floor is at least ~1100/min, well above any normal call pattern. Monthly
quota body shape is pinned in `fixtures/sosovalue_error_monthly_quota.json`.
Substring match on `"Monthly quota"` keeps us safe even if exact wording
drifts (see `_classify_and_raise_429` docstring).
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

# How to onboard a new asset (e.g. SOL, BNB)
# -------------------------------------------
# Wave 1 hardcodes BTC + ETH because that's all SoSoValue's ETF-flows endpoint
# covers today. Adding a third asset is a multi-file touch — these are every
# location that needs editing, in the order a change naturally propagates from
# adapter → pipeline → API → UI:
#
#   backend/etfpulse/adapters/sosovalue.py
#     - add <ASSET>_CURRENCY_ID constant (this file, just below)
#     - extend _currency_id() resolver — drop the BTC/ETH ternary for a dict
#     - widen the `Literal["BTC", "ETH"]` annotations on get_spot_price /
#       get_daily_klines / get_etf_flows
#   backend/etfpulse/adapters/binance.py
#     - add an entry to _SYMBOL (e.g. "SOL": "SOLUSDT")
#     - widen the `Literal["BTC", "ETH"]` annotations
#   backend/etfpulse/pipeline/signal_builder.py
#     - extend _ASSETS tuple + Literal
#   backend/etfpulse/pipeline/prices.py
#     - extend the `Asset` Literal alias
#   backend/etfpulse/api/schemas/signals.py
#     - extend the `AssetLiteral` Literal alias (shared by routes/signals.py
#       AND routes/track_record.py — single source of truth since Stage 8-P6)
#   backend/etfpulse/bot/handlers/_common.py
#     - add to _VALID_ASSETS set
#   backend/fixtures/
#     - add sosovalue_etf_flows_<asset>.json, sosovalue_market_snapshot_<asset>.json,
#       sosovalue_klines_<asset>.json, binance_klines_<asset>.json
#   frontend/src/api/types.ts
#     - extend the `AssetSymbol` union
#   frontend/src/components/signals/AssetBadge.tsx
#     - add a brand-color branch (replace the BTC/ETH ternary with a switch
#       or token map at this point)
#   frontend/src/components/signals/FilterBar.tsx
#     - add a FilterPill for the new asset
#
# When this list gets hit for real OR a 3rd asset arrives, extract a registry
# (e.g. etfpulse/models/assets.py) so all of the above collapse into one source
# of truth. Don't abstract until then — premature registries hide the seams.

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


class MarketSnapshot(BaseModel):
    """Live market snapshot (source: GET /currencies/{id}/market-snapshot).

    Only `price` is load-bearing for Wave 1. Other fields (turnover, supply,
    ATH, etc.) are intentionally ignored so schema drift doesn't break signal
    building over a single field change.

    Response-shape caveat: the SoSoValue docs show the payload flat, but every
    other endpoint's real response is wrapped in `{code, message, data, ...}`.
    The adapter parses `raw.get("data")` consistent with that pattern; if the
    inference is wrong we'll see NULL prices + warnings rather than a crash.
    Verified against fixtures for ETF flows and news; market-snapshot itself
    was un-spikeable (monthly quota exhausted).
    """

    model_config = ConfigDict(extra="ignore")

    price: Decimal


class KlinePoint(BaseModel):
    """One daily OHLC bar (source: GET /currencies/{id}/klines?interval=1d).

    The adapter only requests `interval=1d` — the docs explicitly say this
    is the only interval supported. History is capped at ~3 months.

    `timestamp` is ms-epoch; `bar_date` converts to a UTC date for matching
    against `Signal.signal_date` during backfill.
    """

    model_config = ConfigDict(frozen=True)

    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None

    @property
    def bar_date(self) -> date:
        return datetime.fromtimestamp(self.timestamp / 1000, tz=UTC).date()


class SectorRow(BaseModel):
    """One row in `data.sector` (source: GET /currencies/sector-spotlight).

    Fields verified live (probe_sector_spotlight.py): `name` is always str,
    both numeric fields are always float (never strings, never null). The
    16 sector `marketcap_dom` values sum to 1.0000 — these are exhaustive
    market-share fractions, not relative-to-something rates.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    change_pct_24h: Decimal
    marketcap_dom: Decimal

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v: Any) -> Any:
        # One observed row is `' PerpDEX'` (leading whitespace). Strip on
        # ingestion so downstream lookups by name don't have to.
        return v.strip() if isinstance(v, str) else v


class SpotlightRow(BaseModel):
    """One row in `data.spotlight`. No `marketcap_dom` field — narrower
    thematic baskets (Solana Ecosystem, ETF Candidates, fund portfolios)
    that don't carry market-share weights.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    change_pct_24h: Decimal

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


class SectorSpotlight(BaseModel):
    """Full /currencies/sector-spotlight payload.

    The endpoint accepts a `currency_id` query param but the response is
    byte-identical regardless of value (verified against BTC id, ETH id,
    no-param, and a garbage string). The adapter calls it without params.
    """

    model_config = ConfigDict(frozen=True)

    sector: list[SectorRow]
    spotlight: list[SpotlightRow]

    def find_sector(self, name: str) -> SectorRow | None:
        """Lookup a sector row by exact name match. Returns None if absent.

        Defensive: nothing in the API contract guarantees a particular name
        is present. Today (verified) the `sector` list contains 16 entries
        including `BTC` and `ETH`, but callers MUST handle absence.
        """
        for row in self.sector:
            if row.name == name:
                return row
        return None


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
        # Price caches — small, short TTL. Spot price is queried at most once
        # per asset per daily cycle; the TTL mostly protects against admin
        # trigger spam during testing.
        self._spot_cache: TTLCache[str, Decimal] = TTLCache(maxsize=5, ttl=60)
        self._klines_cache: TTLCache[str, list[KlinePoint]] = TTLCache(maxsize=20, ttl=3600)
        # Sector-spotlight changes slowly (sector weights drift over weeks).
        # 24h TTL matches macro_cache; one fetch per daily cycle in the
        # steady state.
        self._sector_cache: TTLCache[str, SectorSpotlight] = TTLCache(maxsize=1, ttl=86400)

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
                # We log the message verbatim so wording drift is visible in
                # ops logs without us having to re-burn quota verifying it.
                # (The new-tier per-minute floor is high enough that the
                # documented "20 req/min" never actually triggers in
                # production; we'd never see a 429 unless we changed our
                # call patterns. See scripts/probe_rate_limit_429.py for
                # the verification record.)
                try:
                    body_for_log = response.json()
                except ValueError:
                    body_for_log = {}
                log.warning(
                    "sosovalue_rate_limited",
                    path=path,
                    backoff=_RATE_LIMIT_BACKOFF_SECONDS,
                    upstream_message=str(body_for_log.get("message", "")),
                    upstream_code=body_for_log.get("code"),
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

        Routing is by **substring match on the `message` field**, not by
        `code` (both 429 paths share code=402901). Substring `"Monthly quota"`
        is verified against `fixtures/sosovalue_error_monthly_quota.json`.
        Per-minute wording on the new tier was not live-verifiable
        (see scripts/probe_rate_limit_429.py — 35 parallel calls/1.88s
        all returned 200, so the per-minute floor is well above ~1100/min
        and we'd never trip it in production).

        Safe by construction: if SoSoValue ever changes monthly-quota
        wording, this matcher returns silently → caller retries once →
        429 fires again → caller raises `SoSoValueRateLimitError`. We
        never crash; worst case is one wasted retry per quota event.
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

    # --- Price data -----------------------------------------------------

    @staticmethod
    def _currency_id(asset: Literal["BTC", "ETH"]) -> str:
        """Resolve an asset symbol to the SoSoValue currency_id.

        Both IDs are verified in docs/API_REFERENCE.md (currency mapping
        table); not guessed. The single source for both IDs is the module-
        level constants at the top of this file.
        """
        return BTC_CURRENCY_ID if asset == "BTC" else ETH_CURRENCY_ID

    async def get_spot_price(self, asset: Literal["BTC", "ETH"]) -> Decimal:
        """Current USD spot price for BTC or ETH.

        Raises `SoSoValueError` (or a subclass) on failure. Callers are
        expected to catch and fall back — see `pipeline/prices.py` for the
        primary→fallback composer.
        """
        cache_key = f"spot_{asset}"
        if cache_key in self._spot_cache:
            return self._spot_cache[cache_key]

        if self.use_fixtures:
            raw = self._load_fixture(f"sosovalue_market_snapshot_{asset.lower()}")
        else:
            raw = await self._request(
                "GET",
                f"/currencies/{self._currency_id(asset)}/market-snapshot",
            )

        # Response shape (inferred — un-spikeable due to quota exhaustion):
        # `{"code": 0, "message": "...", "data": {"price": ..., ...}, "details": null}`.
        # If the inference is wrong, `data` is None/empty → explicit error.
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            raise SoSoValueError(f"market-snapshot for {asset}: unexpected response envelope")

        snapshot = MarketSnapshot.model_validate(data)
        self._spot_cache[cache_key] = snapshot.price
        log.info("sosovalue_spot_price_fetched", asset=asset, price=str(snapshot.price))
        return snapshot.price

    async def get_daily_klines(
        self,
        asset: Literal["BTC", "ETH"],
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 100,
    ) -> list[KlinePoint]:
        """Daily OHLC klines for BTC or ETH. Only 1d interval is supported.

        Returns newest-first or oldest-first depending on the API (not
        contractually specified in the docs); callers that need a specific
        ordering should sort on `KlinePoint.timestamp` themselves.

        Used by the one-shot historical backfill in
        `scripts/backfill_signal_prices.py` — NOT called from the daily
        cycle (which uses live spot prices via `get_spot_price`).
        """
        cache_key = f"klines_{asset}_{start_time_ms}_{end_time_ms}_{limit}"
        if cache_key in self._klines_cache:
            return self._klines_cache[cache_key]

        if self.use_fixtures:
            raw = self._load_fixture(f"sosovalue_klines_{asset.lower()}")
        else:
            params: dict[str, Any] = {"interval": "1d", "limit": limit}
            if start_time_ms is not None:
                params["start_time"] = start_time_ms
            if end_time_ms is not None:
                params["end_time"] = end_time_ms
            raw = await self._request(
                "GET",
                f"/currencies/{self._currency_id(asset)}/klines",
                params=params,
            )

        # Inferred envelope: `{"code": 0, "data": [{...}, ...], ...}`.
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list):
            raise SoSoValueError(f"klines for {asset}: unexpected response envelope")

        klines = [KlinePoint.model_validate(item) for item in data]
        self._klines_cache[cache_key] = klines
        log.info("sosovalue_klines_fetched", asset=asset, count=len(klines))
        return klines

    async def get_sector_spotlight(self) -> SectorSpotlight:
        """Sector + thematic-basket dominance and 24h returns.

        No query params: the endpoint's `currency_id` parameter is ignored
        by the API (verified — bodies identical across BTC id, ETH id, no
        param, and garbage string).

        Cached 24h. Caller chains: `regime_monitor.classify_regime` reads
        `find_sector("BTC").marketcap_dom` to derive btc_dominance.
        """
        cache_key = "sector_spotlight"
        if cache_key in self._sector_cache:
            return self._sector_cache[cache_key]

        if self.use_fixtures:
            raw = self._load_fixture("sosovalue_sector_spotlight")
        else:
            raw = await self._request("GET", "/currencies/sector-spotlight")

        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            raise SoSoValueError("sector-spotlight: unexpected response envelope")

        spotlight = SectorSpotlight.model_validate(data)
        self._sector_cache[cache_key] = spotlight
        log.info(
            "sosovalue_sector_spotlight_fetched",
            sector_count=len(spotlight.sector),
            spotlight_count=len(spotlight.spotlight),
        )
        return spotlight


# Module-level singleton. Tests that need isolation should instantiate
# SoSoValueClient() directly rather than using this.
sosovalue_client = SoSoValueClient()
