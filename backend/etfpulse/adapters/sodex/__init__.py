"""SoDEX adapter package (Stage 09).

This package owns:
  - **D.1** — EIP-712 typed-data builders (`builders.py`, `schemas.py`,
    `payload.py`, `eip712.py`, `serialization.py`).
  - **D.2** — HTTP clients for spot + perps venues (`spot_client.py`,
    `perps_client.py`, `_http.py`, `responses.py`).

The production backend NEVER holds, generates, or signs with private
keys — signing happens wallet-side via wagmi/viem. D.1 builds the
typed-data dict the wallet will sign; D.2 submits the resulting
signed payload over HTTP. Together they form the production-side
half of the SoDEX execution surface; D.3 (risk controller +
execution pipeline) and D.4 (API routes + Execute page) consume this
package.

Anti-drift rule 27 (CLAUDE.md): nothing under this package may import
`eth_account`, `web3.auto.signing`, `sign_message`, `sign_typed_data`,
or any other signing primitive. The package is build-only by design;
the test suite enforces this via grep at
`tests/test_adapters/test_sodex_typed_data.py::TestAntiDriftRule27`.
"""

from etfpulse.adapters.sodex._http import (
    SodexAuthError,
    SodexEnvelopeError,
    SodexError,
    SodexHttpClient,
    SodexHttpError,
    SodexParseError,
    SodexRateLimitError,
    SodexValidationError,
    estimate_request_weight,
)
from etfpulse.adapters.sodex.builders import (
    TypedDataBundle,
    build_perps_cancel_order,
    build_perps_new_order,
    build_spot_cancel_order,
    build_spot_new_order,
)
from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.responses import (
    AccountBalances,
    APIKey,
    BalanceEntry,
    BookTicker,
    Coin,
    FeeRate,
    MiniTicker,
    OpenOrdersResponse,
    OpenPositionsResponse,
    OrderResponseItem,
    PerpsAccountState,
    PerpsMarkPrice,
    PerpsSymbol,
    SpotAccountState,
    SpotSymbol,
    Ticker,
    TransferResponse,
)
from etfpulse.adapters.sodex.schemas import (
    ClOrdID,
    DecimalString,
    OrderModifier,
    OrderSide,
    OrderType,
    PerpsCancelItem,
    PerpsCancelOrderRequest,
    PerpsNewOrderRequest,
    PerpsOrderItem,
    PositionSide,
    SpotBatchCancelOrderRequest,
    SpotBatchNewOrderRequest,
    SpotCancelItem,
    SpotNewOrderItem,
    StopType,
    TimeInForce,
    TriggerType,
)
from etfpulse.adapters.sodex.spot_client import SodexSpotClient

__all__ = [
    # D.1 — Composers (public API surface).
    "TypedDataBundle",
    "build_spot_new_order",
    "build_spot_cancel_order",
    "build_perps_new_order",
    "build_perps_cancel_order",
    # D.1 — Enums (int-valued — match SoDEX schema.md exactly).
    "OrderSide",
    "OrderType",
    "TimeInForce",
    "OrderModifier",
    "PositionSide",
    "StopType",
    "TriggerType",
    # D.1 — Annotated string types with validation.
    "DecimalString",
    "ClOrdID",
    # D.1 — Spot request schemas.
    "SpotNewOrderItem",
    "SpotBatchNewOrderRequest",
    "SpotCancelItem",
    "SpotBatchCancelOrderRequest",
    # D.1 — Perps request schemas.
    "PerpsOrderItem",
    "PerpsNewOrderRequest",
    "PerpsCancelItem",
    "PerpsCancelOrderRequest",
    # D.2 — Venue clients.
    "SodexSpotClient",
    "SodexPerpsClient",
    # D.2 — HTTP core + errors.
    "SodexHttpClient",
    "SodexError",
    "SodexHttpError",
    "SodexRateLimitError",
    "SodexEnvelopeError",
    "SodexAuthError",
    "SodexValidationError",
    "SodexParseError",
    "estimate_request_weight",
    # NOTE: `_helpers.py` symbols (lower_address, signed_write_headers,
    # validate_order_response_items) are intentionally NOT re-exported.
    # They're package-internal — only the two venue clients import them.
    # D.3/D.4 interact via SodexSpotClient / SodexPerpsClient methods,
    # never directly with the helpers.
    # D.2 — Response DTOs (mirror gateway shape).
    "SpotSymbol",
    "PerpsSymbol",
    "Coin",
    "Ticker",
    "MiniTicker",
    "BookTicker",
    "PerpsMarkPrice",
    "BalanceEntry",
    "AccountBalances",
    "SpotAccountState",
    "PerpsAccountState",
    "OpenOrdersResponse",
    "OpenPositionsResponse",
    "FeeRate",
    "APIKey",
    "OrderResponseItem",
    "TransferResponse",
]
