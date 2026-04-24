"""Binance public market-data adapter — backup spot price + historical klines.

Exists because `adapters/sosovalue.py` is a single point of failure for
`Signal.price_at_creation` (issue #34). SoSoValue's 100k/month shared quota
is easy to exhaust via flow/news ingestion; when that happens, the signal
pipeline can still detect + alert, but prices go NULL, which in turn breaks
Stage 08 track record (issue #44).

Auth: none. These are Binance's public market-data endpoints — no key,
no signature, no account. We hit `data-api.binance.vision` (Binance's
market-data-only mirror) rather than `api.binance.com` because the latter
is geo-blocked in some deploy environments. The `.vision` host is the
official mirror described in Binance's public data docs.

Symbols: `BTCUSDT`, `ETHUSDT`. USDT ≠ USD (~±10bp drift vs aggregator
pricing) but for percentage-return calculations on signal outcomes the
tether basis is negligible. If we ever need fiat-accurate reporting we
can switch to `BTCBUSD` (deprecated) or a different exchange.

Response shapes (verified live, 2026-04-24):
    GET /api/v3/ticker/price?symbol=BTCUSDT
        → {"symbol": "BTCUSDT", "price": "77602.07000000"}
    GET /api/v3/klines?symbol=BTCUSDT&interval=1d&limit=N
        → [[open_time_ms, open, high, low, close, volume, close_time_ms,
            quote_asset_volume, trades_count, taker_buy_base,
            taker_buy_quote, ignore], ...]
        All numeric fields are string-encoded decimals.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import structlog
from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict

from etfpulse.config import settings

log = structlog.get_logger()


FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"
_REQUEST_TIMEOUT = 15.0

# Asset → Binance spot symbol. USDT pair is the most liquid; using it trades
# a small amount of fiat-precision noise for reliability.
_SYMBOL: dict[Literal["BTC", "ETH"], str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
}


class BinanceError(Exception):
    """Any failure in the Binance adapter. Caller treats as non-fatal fallback miss."""


class BinanceKline(BaseModel):
    """One daily OHLC bar (source: GET /api/v3/klines?interval=1d).

    Wire shape is a 12-element array of strings/numbers, not a named object,
    so construction goes through `from_raw` rather than direct validation.
    """

    model_config = ConfigDict(frozen=True)

    open_time: int  # ms epoch
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int  # ms epoch

    @classmethod
    def from_raw(cls, row: list[Any]) -> BinanceKline:
        """Parse one 12-element array from the klines response."""
        if len(row) < 7:
            raise BinanceError(f"kline row too short: {len(row)} elements")
        return cls(
            open_time=int(row[0]),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            close_time=int(row[6]),
        )

    @property
    def bar_date(self) -> date:
        """UTC calendar date of the bar open. Used to join against Signal.signal_date."""
        return datetime.fromtimestamp(self.open_time / 1000, tz=UTC).date()


class BinanceClient:
    """Thin async wrapper over Binance's public market-data endpoints."""

    def __init__(self) -> None:
        self.base_url = settings.binance_base_url.rstrip("/")
        self._spot_cache: TTLCache[str, Decimal] = TTLCache(maxsize=5, ttl=60)
        self._klines_cache: TTLCache[str, list[BinanceKline]] = TTLCache(
            maxsize=20, ttl=3600
        )

    @property
    def use_fixtures(self) -> bool:
        """Read live from settings so tests can flip the flag after init."""
        return settings.binance_use_fixtures

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """One GET. No auth headers. Any non-2xx or network error raises BinanceError."""
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(url, params=params or {})
        except httpx.RequestError as exc:
            log.warning("binance_network_error", path=path, error=str(exc))
            raise BinanceError(f"network error: {exc}") from exc

        if response.status_code >= 400:
            log.warning(
                "binance_http_error",
                path=path,
                status=response.status_code,
                body=response.text[:500],
            )
            raise BinanceError(
                f"HTTP {response.status_code} on {path}: {response.text[:200]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise BinanceError(f"invalid JSON from {path}: {exc}") from exc

    @staticmethod
    def _load_fixture(name: str) -> Any:
        fixture_path = FIXTURES_DIR / f"{name}.json"
        if not fixture_path.exists():
            log.warning("binance_fixture_missing", name=name, path=str(fixture_path))
            return None
        return json.loads(fixture_path.read_text())

    # --- Public methods -----------------------------------------------------

    async def get_spot_price(self, asset: Literal["BTC", "ETH"]) -> Decimal:
        """Current spot price for BTC or ETH (USDT-denominated).

        Raises `BinanceError` on any failure. Callers fall back or log + NULL.
        """
        cache_key = f"spot_{asset}"
        if cache_key in self._spot_cache:
            return self._spot_cache[cache_key]

        symbol = _SYMBOL[asset]
        if self.use_fixtures:
            raw = self._load_fixture(f"binance_ticker_{asset.lower()}")
        else:
            raw = await self._request("/api/v3/ticker/price", params={"symbol": symbol})

        if not isinstance(raw, dict) or "price" not in raw:
            raise BinanceError(f"unexpected ticker/price response for {symbol}: {raw!r}")

        try:
            price = Decimal(str(raw["price"]))
        except (ValueError, ArithmeticError) as exc:
            raise BinanceError(f"unparseable price for {symbol}: {raw['price']!r}") from exc

        self._spot_cache[cache_key] = price
        log.info("binance_spot_price_fetched", asset=asset, price=str(price))
        return price

    async def get_daily_klines(
        self,
        asset: Literal["BTC", "ETH"],
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 100,
    ) -> list[BinanceKline]:
        """Daily OHLC bars for BTC/ETH. Returned oldest-first per Binance convention."""
        cache_key = f"klines_{asset}_{start_time_ms}_{end_time_ms}_{limit}"
        if cache_key in self._klines_cache:
            return self._klines_cache[cache_key]

        symbol = _SYMBOL[asset]
        if self.use_fixtures:
            raw = self._load_fixture(f"binance_klines_{asset.lower()}")
        else:
            params: dict[str, Any] = {
                "symbol": symbol,
                "interval": "1d",
                "limit": limit,
            }
            if start_time_ms is not None:
                params["startTime"] = start_time_ms
            if end_time_ms is not None:
                params["endTime"] = end_time_ms
            raw = await self._request("/api/v3/klines", params=params)

        if not isinstance(raw, list):
            raise BinanceError(f"unexpected klines response for {symbol}: type={type(raw).__name__}")

        klines = [BinanceKline.from_raw(cast(list[Any], row)) for row in raw]
        self._klines_cache[cache_key] = klines
        log.info("binance_klines_fetched", asset=asset, count=len(klines))
        return klines


# Module-level singleton. Tests that need isolation should instantiate
# BinanceClient() directly rather than using this.
binance_client = BinanceClient()
