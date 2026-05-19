"""`SodexPerpsClient` — typed venue client for SoDEX perps endpoints.

Mirrors `SodexSpotClient` but for the perps venue. The protocol
surface is similar (same envelope shape, same auth-header trio, same
batch dual-layer response) with these differences:

  - Perps adds endpoints: `/markets/mark-prices`, `/accounts/{addr}/positions`.
  - Perps trade paths differ: `POST /trade/orders` (no `/batch` suffix
    — the API design carries `orders` as a list anyway). Same for the
    cancel endpoint `DELETE /trade/orders`.
  - Perps EIP-712 domain is `"futures"` (not `"perps"`) — the
    composer in `builders.py` already handles that mapping; this
    client just submits the bytes.
  - Account state has more fields (margin variants) and additional
    nullable arrays (`P` positions, `S` sub-state).

Anti-drift rule 27 still holds — no signing primitives imported.
"""

from __future__ import annotations

from etfpulse.adapters.sodex._helpers import (
    lower_address,
    signed_write_headers,
    validate_order_response_items,
)
from etfpulse.adapters.sodex._http import SodexHttpClient
from etfpulse.adapters.sodex.responses import (
    AccountBalances,
    APIKey,
    BookTicker,
    Coin,
    FeeRate,
    OpenOrdersResponse,
    OpenPositionsResponse,
    OrderResponseItem,
    PerpsAccountState,
    PerpsMarkPrice,
    PerpsSymbol,
    Ticker,
)


class SodexPerpsClient:
    """Async client for the SoDEX perps venue. Same lifecycle as
    `SodexSpotClient` — use via async context manager or call
    `close()` explicitly."""

    def __init__(self, http: SodexHttpClient) -> None:
        self._http = http

    @classmethod
    def from_settings(cls) -> SodexPerpsClient:
        from etfpulse.config import settings

        http = SodexHttpClient(
            base_url=settings.sodex_resolved_perps_base_url,
            timeout_seconds=settings.sodex_http_timeout_seconds,
            retry_max_attempts=settings.sodex_http_retry_max_attempts,
            retry_base_seconds=settings.sodex_http_retry_base_seconds,
        )
        return cls(http)

    async def __aenter__(self) -> SodexPerpsClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.close()

    # -----------------------------------------------------------------
    # Market reads
    # -----------------------------------------------------------------

    async def get_symbols(self) -> list[PerpsSymbol]:
        data = await self._http.get("/markets/symbols")
        return [PerpsSymbol.model_validate(row) for row in (data or [])]

    async def get_coins(self) -> list[Coin]:
        data = await self._http.get("/markets/coins")
        return [Coin.model_validate(row) for row in (data or [])]

    async def get_tickers(self) -> list[Ticker]:
        data = await self._http.get("/markets/tickers")
        return [Ticker.model_validate(row) for row in (data or [])]

    async def get_book_tickers(self) -> list[BookTicker]:
        """Perps exposes the same book-ticker shape as spot."""
        data = await self._http.get("/markets/bookTickers")
        return [BookTicker.model_validate(row) for row in (data or [])]

    async def get_mark_prices(self) -> list[PerpsMarkPrice]:
        """Perps-only — funding rate + mark price + index price snap."""
        data = await self._http.get("/markets/mark-prices")
        return [PerpsMarkPrice.model_validate(row) for row in (data or [])]

    # -----------------------------------------------------------------
    # Account reads
    # -----------------------------------------------------------------

    async def get_balances(self, address: str) -> AccountBalances:
        data = await self._http.get(f"/accounts/{lower_address(address)}/balances")
        return AccountBalances.model_validate(data)

    async def get_state(self, address: str) -> PerpsAccountState:
        """Compact perps state — includes margin fields not in spot."""
        data = await self._http.get(f"/accounts/{lower_address(address)}/state")
        return PerpsAccountState.model_validate(data)

    async def get_open_orders(self, address: str) -> OpenOrdersResponse:
        data = await self._http.get(f"/accounts/{lower_address(address)}/orders")
        return OpenOrdersResponse.model_validate(data)

    async def get_positions(self, address: str) -> OpenPositionsResponse:
        """Perps-only — open derivatives positions. The DTO transparently
        renames the wire's misleading `orders` key (gateway quirk) to
        `positions`."""
        data = await self._http.get(f"/accounts/{lower_address(address)}/positions")
        return OpenPositionsResponse.model_validate(data)

    async def get_fee_rate(self, address: str) -> FeeRate:
        data = await self._http.get(f"/accounts/{lower_address(address)}/fee-rate")
        return FeeRate.model_validate(data)

    async def get_api_keys(self, address: str) -> list[APIKey]:
        data = await self._http.get(f"/accounts/{lower_address(address)}/api-keys")
        return [APIKey.model_validate(row) for row in (data or [])]

    # -----------------------------------------------------------------
    # Signed writes
    # -----------------------------------------------------------------

    async def submit_batch_new_order(
        self,
        *,
        body_bytes: bytes,
        typed_signature: str,
        nonce: int,
        api_key_name: str,
    ) -> list[OrderResponseItem]:
        """`POST /trade/orders` — submit a batch of perps orders.

        Note the path — `/trade/orders` (NO `/batch` suffix), unlike
        spot's `/trade/orders/batch`. The body still wraps a list of
        orders; the API path doesn't reflect that, but per api.md the
        request struct is `PerpsNewOrderRequest{accountID, symbolID,
        orders[]}`. Same byte-exact body contract as spot."""
        data = await self._http.post(
            "/trade/orders",
            body_bytes=body_bytes,
            headers=signed_write_headers(
                api_key_name=api_key_name,
                typed_signature=typed_signature,
                nonce=nonce,
            ),
        )
        return validate_order_response_items(data)

    async def submit_batch_cancel_order(
        self,
        *,
        body_bytes: bytes,
        typed_signature: str,
        nonce: int,
        api_key_name: str,
    ) -> list[OrderResponseItem]:
        """`DELETE /trade/orders` — submit perps cancels. Same envelope
        + same per-order shape as spot. V.3 captured the rejection
        case (`order rejected: OrderNotFound`)."""
        data = await self._http.delete(
            "/trade/orders",
            body_bytes=body_bytes,
            headers=signed_write_headers(
                api_key_name=api_key_name,
                typed_signature=typed_signature,
                nonce=nonce,
            ),
        )
        return validate_order_response_items(data)
