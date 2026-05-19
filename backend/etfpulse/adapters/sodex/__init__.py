"""SoDEX adapter package (Stage 09).

This package owns the EIP-712 typed-data builders + (later) the HTTP
clients that talk to the SoDEX spot and perps gateways. The production
backend NEVER holds, generates, or signs with private keys — signing
happens wallet-side via wagmi/viem; this package only builds the
typed-data dict the wallet will sign, and submits the resulting signed
payload.

PR D.1 ships the schemas + payload builders. D.2 adds the HTTP clients.

Anti-drift rule 27 (CLAUDE.md): nothing under this package may import
`eth_account`, `web3.auto.signing`, or any other signing primitive. The
package is build-only by design; the test suite enforces this.
"""

from etfpulse.adapters.sodex.builders import (
    TypedDataBundle,
    build_perps_cancel_order,
    build_perps_new_order,
    build_spot_cancel_order,
    build_spot_new_order,
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

__all__ = [
    # Composers (public API surface).
    "TypedDataBundle",
    "build_spot_new_order",
    "build_spot_cancel_order",
    "build_perps_new_order",
    "build_perps_cancel_order",
    # Enums (int-valued — match SoDEX schema.md exactly).
    "OrderSide",
    "OrderType",
    "TimeInForce",
    "OrderModifier",
    "PositionSide",
    "StopType",
    "TriggerType",
    # Annotated string types with validation.
    "DecimalString",
    "ClOrdID",
    # Spot request schemas.
    "SpotNewOrderItem",
    "SpotBatchNewOrderRequest",
    "SpotCancelItem",
    "SpotBatchCancelOrderRequest",
    # Perps request schemas.
    "PerpsOrderItem",
    "PerpsNewOrderRequest",
    "PerpsCancelItem",
    "PerpsCancelOrderRequest",
]
