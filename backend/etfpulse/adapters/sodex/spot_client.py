"""`SodexSpotClient` — typed venue client for SoDEX spot endpoints.

Public API of D.2. Wraps `SodexHttpClient` with one method per
documented endpoint, returning the typed DTOs from `responses.py`.

The contract for writes
-----------------------
The backend NEVER signs. Write methods accept:

  - `body_bytes`: the EXACT bytes that were signed wallet-side. Must
    match what `D.1.compact_json(request_model)` produced. Re-deriving
    on the server is the wrong shape (gateway re-hashes `body_bytes`,
    diverging bytes → signature mismatch).
  - `typed_signature`: `0x01` + 65-byte ECDSA sig in hex (per api.md
    §"Typed signature"). The frontend uses wagmi/viem; D.4 will
    normalise `v ∈ {27, 28}` → `v ∈ {0, 1}` before this layer sees it
    (V.3 confirmed the gateway expects raw `v`).
  - `nonce`: Unix-ms within `(T-2d, T+1d)` per api.md §"Sodex nonces".
  - `api_key_name`: the NAME of the registered key (e.g. `"default"`),
    NOT the EVM address. Per api.md (web version) — the local docs
    snapshot misleads on this.

Anti-drift rule 27: no signing primitives imported. Verified by the
grep test in `test_sodex_typed_data.py::TestAntiDriftRule27`.
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
    MiniTicker,
    OpenOrdersResponse,
    OrderResponseItem,
    SpotAccountState,
    SpotSymbol,
    Ticker,
)


class SodexSpotClient:
    """Async client for the SoDEX spot venue. One instance per process
    is normal — `httpx.AsyncClient` handles connection pooling. Use
    via async context manager to ensure the underlying client closes:

        async with SodexSpotClient.from_settings() as client:
            balances = await client.get_balances(addr)

    Direct construction is also supported (D.4's tests will use a
    bound httpx mock transport)."""

    def __init__(self, http: SodexHttpClient) -> None:
        self._http = http

    @classmethod
    def from_settings(cls) -> SodexSpotClient:
        """Construct with values from `etfpulse.config.settings`.
        Returns a ready-to-use client; caller owns the close lifecycle
        (or uses the async context manager)."""
        # Import inside the classmethod to avoid a top-level import
        # that pulls `etfpulse.config` into every test of this module
        # — the construction path is opt-in.
        from etfpulse.config import settings

        http = SodexHttpClient(
            base_url=settings.sodex_resolved_spot_base_url,
            timeout_seconds=settings.sodex_http_timeout_seconds,
            retry_max_attempts=settings.sodex_http_retry_max_attempts,
            retry_base_seconds=settings.sodex_http_retry_base_seconds,
        )
        return cls(http)

    async def __aenter__(self) -> SodexSpotClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.close()

    # -----------------------------------------------------------------
    # Market reads
    # -----------------------------------------------------------------

    async def get_symbols(self) -> list[SpotSymbol]:
        data = await self._http.get("/markets/symbols")
        return [SpotSymbol.model_validate(row) for row in (data or [])]

    async def get_coins(self) -> list[Coin]:
        data = await self._http.get("/markets/coins")
        return [Coin.model_validate(row) for row in (data or [])]

    async def get_tickers(self) -> list[Ticker]:
        data = await self._http.get("/markets/tickers")
        return [Ticker.model_validate(row) for row in (data or [])]

    async def get_mini_tickers(self) -> list[MiniTicker]:
        data = await self._http.get("/markets/miniTickers")
        return [MiniTicker.model_validate(row) for row in (data or [])]

    async def get_book_tickers(self) -> list[BookTicker]:
        data = await self._http.get("/markets/bookTickers")
        return [BookTicker.model_validate(row) for row in (data or [])]

    # -----------------------------------------------------------------
    # Account reads — all take the wallet address as path param
    # -----------------------------------------------------------------

    async def get_balances(self, address: str) -> AccountBalances:
        data = await self._http.get(f"/accounts/{lower_address(address)}/balances")
        return AccountBalances.model_validate(data)

    async def get_state(self, address: str) -> SpotAccountState:
        """Compact state shape — used by frontends for quick polling.
        Includes the `aid` field the signed-write request bodies need."""
        data = await self._http.get(f"/accounts/{lower_address(address)}/state")
        return SpotAccountState.model_validate(data)

    async def get_open_orders(self, address: str) -> OpenOrdersResponse:
        data = await self._http.get(f"/accounts/{lower_address(address)}/orders")
        return OpenOrdersResponse.model_validate(data)

    async def get_fee_rate(self, address: str) -> FeeRate:
        data = await self._http.get(f"/accounts/{lower_address(address)}/fee-rate")
        return FeeRate.model_validate(data)

    async def get_api_keys(self, address: str) -> list[APIKey]:
        """Per api.md L22: returns the named EVM keys registered for
        the account. The `name` field of each entry is what goes in
        the `X-API-Key` header on signed writes. D.4 reads this to
        bind a wallet to its key name."""
        data = await self._http.get(f"/accounts/{lower_address(address)}/api-keys")
        return [APIKey.model_validate(row) for row in (data or [])]

    # -----------------------------------------------------------------
    # Signed writes — accept pre-signed bytes + signature, never sign
    # -----------------------------------------------------------------

    async def submit_batch_new_order(
        self,
        *,
        body_bytes: bytes,
        typed_signature: str,
        nonce: int,
        api_key_name: str,
    ) -> list[OrderResponseItem]:
        """`POST /trade/orders/batch` — submit a batch of spot orders.

        `body_bytes` must equal `D.1.compact_json(SpotBatchNewOrderRequest)`
        — the same bytes the wallet signed. The outer envelope's
        success means the GATEWAY accepted the request; each inner
        `OrderResponseItem.code` reflects per-order acceptance.
        Per-order failures are NOT raised — the caller decides what
        to do with a partial batch."""
        data = await self._http.post(
            "/trade/orders/batch",
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
        """`DELETE /trade/orders/batch` — submit a batch of spot cancels.

        Same dual-layer envelope as new-order: outer `code: 0` means
        the gateway accepted the cancel request; each inner item
        carries `code` and (on rejection) `error`. Common rejection:
        `"order rejected: OrderNotFound"` for stale clOrdIDs."""
        data = await self._http.delete(
            "/trade/orders/batch",
            body_bytes=body_bytes,
            headers=signed_write_headers(
                api_key_name=api_key_name,
                typed_signature=typed_signature,
                nonce=nonce,
            ),
        )
        return validate_order_response_items(data)
