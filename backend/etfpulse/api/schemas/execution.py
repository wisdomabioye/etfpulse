"""Pydantic shapes for the PR D.4.3 execution surface.

These are the API boundary — strings on the wire, mapped to D.1 SoDEX
IntEnums inside the route handler before the call into D.3's risk +
pipeline. The map lives in this module (not on the orchestrator) so
adding a new API enum value doesn't ripple into `pipeline/execution/`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from etfpulse.adapters.sodex.schemas import (
    OrderSide as SodexOrderSide,
)
from etfpulse.adapters.sodex.schemas import (
    OrderType as SodexOrderType,
)
from etfpulse.adapters.sodex.schemas import (
    PositionSide as SodexPositionSide,
)
from etfpulse.adapters.sodex.schemas import (
    TimeInForce as SodexTimeInForce,
)
from etfpulse.adapters.sodex.schemas import (
    TriggerType as SodexTriggerType,
)
from etfpulse.models.order import Venue

# ---------------------------------------------------------------------------
# Wire enums (lowercase strings) + map to SoDEX IntEnums
# ---------------------------------------------------------------------------


class ApiSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ApiOrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class ApiTimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    GTX = "gtx"


class ApiPositionSide(StrEnum):
    BOTH = "both"
    LONG = "long"
    SHORT = "short"


class ApiTriggerType(StrEnum):
    MARK_PRICE = "mark_price"
    LAST_PRICE = "last_price"
    INDEX_PRICE = "index_price"


# Single-source-of-truth maps. Adding a new wire-enum value: add to the
# StrEnum above + add the mapping here. The risk gate downstream catches
# any value the gateway doesn't actually accept (e.g., FOK is on the
# enum but `_UNSUPPORTED_TIME_IN_FORCE` rejects it).
_API_TO_SODEX_SIDE: dict[str, int] = {
    ApiSide.BUY.value: SodexOrderSide.BUY.value,
    ApiSide.SELL.value: SodexOrderSide.SELL.value,
}
_API_TO_SODEX_TYPE: dict[str, int] = {
    ApiOrderType.LIMIT.value: SodexOrderType.LIMIT.value,
    ApiOrderType.MARKET.value: SodexOrderType.MARKET.value,
}
_API_TO_SODEX_TIF: dict[str, int] = {
    ApiTimeInForce.GTC.value: SodexTimeInForce.GTC.value,
    ApiTimeInForce.IOC.value: SodexTimeInForce.IOC.value,
    ApiTimeInForce.FOK.value: SodexTimeInForce.FOK.value,
    ApiTimeInForce.GTX.value: SodexTimeInForce.GTX.value,
}
_API_TO_SODEX_POSITION_SIDE: dict[str, int] = {
    ApiPositionSide.BOTH.value: SodexPositionSide.BOTH.value,
    ApiPositionSide.LONG.value: SodexPositionSide.LONG.value,
    ApiPositionSide.SHORT.value: SodexPositionSide.SHORT.value,
}
_API_TO_SODEX_TRIGGER_TYPE: dict[str, int] = {
    ApiTriggerType.MARK_PRICE.value: SodexTriggerType.MARK_PRICE.value,
    ApiTriggerType.LAST_PRICE.value: SodexTriggerType.LAST_PRICE.value,
    ApiTriggerType.INDEX_PRICE.value: SodexTriggerType.INDEX_PRICE.value,
}


def api_side_to_sodex(v: str) -> int:
    return _API_TO_SODEX_SIDE[v]


def api_order_type_to_sodex(v: str) -> int:
    return _API_TO_SODEX_TYPE[v]


def api_tif_to_sodex(v: str) -> int:
    return _API_TO_SODEX_TIF[v]


def api_position_side_to_sodex(v: str) -> int:
    return _API_TO_SODEX_POSITION_SIDE[v]


def api_trigger_type_to_sodex(v: str) -> int:
    return _API_TO_SODEX_TRIGGER_TYPE[v]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class PrepareNewRequest(BaseModel):
    """`POST /api/execution/prepare` body.

    Mirrors `pipeline.execution.risk.RiskRequest` with wire-friendly
    strings instead of SoDEX IntEnums. Decimals come through as JSON
    numbers/strings; Pydantic v2 accepts both transparently.

    Asset is uppercased on the wire (`"BTC"` / `"ETH"`) — risk gate
    enforces the supported set.
    """

    venue: str = Field(..., description="Venue enum value (sodex_spot | sodex_perps)")
    asset: str = Field(..., min_length=1, max_length=10)
    side: ApiSide
    order_type: ApiOrderType
    time_in_force: ApiTimeInForce
    requested_size: Decimal = Field(..., gt=0)
    requested_price: Decimal | None = Field(default=None, gt=0)
    # Perps-only fields. Spot orders MUST leave these unset (or
    # explicitly None); risk gate enforces.
    position_side: ApiPositionSide | None = None
    trigger_type: ApiTriggerType | None = None
    leverage: Decimal | None = Field(default=None, gt=0)
    is_conditional: bool = False
    # Optional linkage to a Signal that drove this order. NULL = ad-hoc.
    signal_id: int | None = Field(default=None, gt=0)


class SubmitRequest(BaseModel):
    """`POST /api/execution/submit/{order_id}` body.

    Signature shape pinned by regex: `0x01` SoDEX type byte + 65-byte
    ECDSA hex (130 lowercase hex chars). The frontend MUST do the
    `v -= 27` normalization + `0x01` prepend before posting (see
    CLAUDE.md "SoDEX HTTP adapters" §wire-contract).
    """

    signature: str = Field(..., pattern=r"^0x01[0-9a-f]{130}$")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PrepareNewResponse(BaseModel):
    """`POST /api/execution/prepare` 200 body.

    Successful path only — risk DENY returns 403 with the reason in
    `detail`; the success body carries the typed-data the wallet
    must sign next.
    """

    order_id: int
    client_order_id: str
    nonce: int
    typed_data: dict


class PrepareCancelResponse(BaseModel):
    """`POST /api/execution/prepare-cancel/{order_id}` 200 body.

    Three shapes:
      - `local_only=True` — PENDING order cancelled in-place; no wallet
        action needed. `typed_data` is None.
      - `replayed=True` — order already terminal (CANCELLED/FILLED/etc).
        No-op. `typed_data` is None.
      - default — `typed_data` carries the cancel typed-data to sign.
    """

    order_id: int
    typed_data: dict | None = None
    client_order_id: str | None = None
    nonce: int | None = None
    local_only: bool = False
    replayed: bool = False


class SubmitResponse(BaseModel):
    """`POST /api/execution/submit{,-cancel}/{order_id}` 200 body."""

    order_id: int
    status: str
    exchange_order_id: str | None = None
    error_message: str | None = None
    replayed: bool = False


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


class OrderOut(BaseModel):
    """Single Order row, serialized for `/orders` + `/orders/{id}`.

    Mirrors `models.Order` columns the FE cares about; omits internal
    EIP-712 payload (TEXT, not user-facing) + raw request/response
    (operator-debug surface, exposed via admin only).
    """

    id: int
    user_id: int | None
    signal_id: int | None
    venue: str
    asset: str
    side: str
    order_type: str
    time_in_force: str
    requested_size: Decimal
    requested_price: Decimal | None
    filled_size: Decimal | None
    filled_price: Decimal | None
    filled_value: Decimal | None
    fees: Decimal | None
    status: str
    exchange_order_id: str | None
    client_order_id: str
    error_message: str | None
    paper_trade: bool
    account_id: int | None
    symbol_id: int | None
    nonce: int | None
    nonce_expires_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PaginatedOrders(BaseModel):
    """`GET /api/execution/orders` paginated response."""

    items: list[OrderOut]
    total: int
    limit: int
    offset: int


class PositionOut(BaseModel):
    """Single Position row."""

    id: int
    user_id: int | None
    venue: str
    asset: str
    side: str
    size: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    status: str
    paper_trade: bool
    leverage: Decimal | None
    opened_at: datetime
    closed_at: datetime | None
    close_price: Decimal | None
    realized_pnl: Decimal | None

    model_config = {"from_attributes": True}


class PositionsResponse(BaseModel):
    """`GET /api/execution/positions` — open positions only."""

    items: list[PositionOut]


class SymbolOut(BaseModel):
    """`GET /api/execution/symbols` row.

    Drives the FE dropdown — venue/asset/name/symbol_id is all the FE
    needs to build an order against the right market. Internal `raw`
    payload from the cache is intentionally not exposed.
    """

    venue: str
    symbol_id: int
    name: str
    asset: str


class SymbolsResponse(BaseModel):
    items: list[SymbolOut]


# ---------------------------------------------------------------------------
# Venue value list (for runtime validation in routes — schema doesn't
# pin venue to an enum because the wire string is `Venue.value`, not
# the Venue enum name).
# ---------------------------------------------------------------------------


VALID_VENUES: frozenset[str] = frozenset(item.value for item in Venue)
