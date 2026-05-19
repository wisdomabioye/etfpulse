"""Pydantic mirrors of the SoDEX Go SDK request structs.

These models are the **typed entry point** for every SoDEX order action.
Their job is twofold:

1. **Validation at construction time.** Field aliases catch camelCase
   typos; `DecimalString` and `ClOrdID` reject malformed values before
   they reach the gateway; enum fields reject out-of-range integers.

2. **Pin field order for byte-exact compatibility with the SoDEX gateway.**
   The gateway re-marshals incoming JSON bodies via Go's `json.Marshal`
   which serializes Go struct fields in *declaration order*. Pydantic v2
   also preserves declaration order in `model_dump`. As long as our
   declaration order here matches the Go SDK's struct order, the compact
   JSON we emit in `serialization.py` (D.1 step 2) will hash to the same
   `payloadHash` the gateway computes — and the wallet's EIP-712
   signature will verify on the first attempt.

The V.1 fixture (`tests/fixtures/sodex_eip712_golden.json`) is the
contract. The golden-test suite (D.1 step 5) asserts that the compact
JSON we emit from these models matches the fixture byte-for-byte.
Changes to ANY field order or alias MUST come with a re-capture from
`scripts/sodex_verify/eip712_capture/` (Go program) and a passing
golden test. See `api.md` §"Important rules for producing a correct
payloadHash" for the underlying serialization invariants.

Authoritative references (in this order of precedence):
  1. `tests/fixtures/sodex_eip712_golden.json` — captured from the
     official Go SDK; byte-exact ground truth.
  2. `docs/sodex/sodex.com/documentation/api/api.md` — explicit field
     order for `PerpsOrderItem`; `omitempty` and `DecimalString` rules.
  3. `docs/sodex/sodex.com/documentation/api/rest-v1/schema.md` — field
     names + types for every other struct.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# ---------------------------------------------------------------------------
# Enums — all integer-valued per schema.md `### *Enum` sections. We use
# Python `IntEnum` so that `model_dump()` with `use_enum_values=True`
# emits the integer (matching the gateway's JSON wire format) while
# Python call sites keep the readable name.
# ---------------------------------------------------------------------------


class OrderSide(IntEnum):
    BUY = 1
    SELL = 2


class OrderType(IntEnum):
    LIMIT = 1
    MARKET = 2


class TimeInForce(IntEnum):
    """Per schema.md §TimeInForceEnum.

    FOK is documented as **not supported yet** by the gateway — defined for
    API parity but should not be used in production.
    """

    GTC = 1
    FOK = 2  # not supported yet — see schema.md
    IOC = 3
    GTX = 4  # post-only


class OrderModifier(IntEnum):
    """Per schema.md §OrderModifierEnum (perps only)."""

    NORMAL = 1
    STOP = 2
    BRACKET = 3
    ATTACHED_STOP = 4


class PositionSide(IntEnum):
    """Per schema.md §PositionSideEnum (perps only).

    Only `BOTH` is currently supported in order placement. `LONG` /
    `SHORT` are reserved for future hedge mode (docs explicitly say
    "not supported in order placement yet"). We expose them for API
    parity but production calls should always use `BOTH`.
    """

    BOTH = 1
    LONG = 2  # not supported yet
    SHORT = 3  # not supported yet


class StopType(IntEnum):
    """Per schema.md §StopTypeEnum (perps only — for TP/SL legs)."""

    STOP_LOSS = 1
    TAKE_PROFIT = 2


class TriggerType(IntEnum):
    """Per schema.md §TriggerTypeEnum (perps only).

    Only `MARK_PRICE` is supported for order placement. `LAST_PRICE` and
    `INDEX_PRICE` are reserved.
    """

    LAST_PRICE = 1  # not supported yet
    MARK_PRICE = 2
    INDEX_PRICE = 3  # not supported yet


# ---------------------------------------------------------------------------
# Annotated string types — Pydantic constraints applied at validation time
# so a malformed value never reaches serialization.
# ---------------------------------------------------------------------------

# `DecimalString` per api.md §"Important rules" #3 — emitted as a quoted
# JSON string, never a numeric. The regex accepts an optional sign, a
# digit run, and an optional fractional part. Scientific notation is
# explicitly rejected — the gateway's Go decoder for `DecimalString`
# uses `*big.Float`-style parsing that does not accept `1e3`.
DecimalString = Annotated[
    str,
    StringConstraints(pattern=r"^-?\d+(\.\d+)?$"),
]

# `ClOrdID` per schema.md — alphanumeric + underscore + hyphen, 1-36 chars.
# The gateway enforces the same pattern; rejecting client-side surfaces
# the failure at construction rather than as a cryptic 4xx.
ClOrdID = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-zA-Z_-]{1,36}$"),
]


# ---------------------------------------------------------------------------
# Shared model config.
#
# `populate_by_name=True`  — accept either snake_case field name or
#                            camelCase alias during construction (lets
#                            tests use the Pythonic name when convenient).
# `extra="forbid"`         — reject unknown keys at construction. Catches
#                            typos like `quanity` before they hit the
#                            gateway and fail mysteriously.
# `use_enum_values=True`   — IntEnum members serialize to their int value
#                            via `model_dump()`. Without this the value
#                            would be the IntEnum instance (still serializes
#                            via json but the type signal would leak).
# `frozen=True`            — order requests are write-once; mutating after
#                            construction breaks the "same bytes the
#                            wallet signed" invariant. Pydantic v2's
#                            `frozen=True` blocks ATTRIBUTE REASSIGNMENT
#                            (`req.account_id = 9999` raises) but does
#                            NOT recursively freeze mutable inner fields
#                            (`req.orders.append(...)` still works). For
#                            our use this is OK: builders capture the
#                            serialized payload_json + payload_hash at
#                            build-time and the bundle holds them as
#                            strings, so post-build mutation of the
#                            input model doesn't affect the bundle's
#                            captured state. Callers MUST NOT, however,
#                            assume that mutating the request and re-
#                            serializing yields the same hash.
# ---------------------------------------------------------------------------

_REQUEST_MODEL_CONFIG = ConfigDict(
    populate_by_name=True,
    extra="forbid",
    use_enum_values=True,
    frozen=True,
)


# ---------------------------------------------------------------------------
# Spot request schemas.
# ---------------------------------------------------------------------------


class SpotNewOrderItem(BaseModel):
    """Mirror of `BatchNewOrderItem` (spot/types).

    Field order: `symbolID, clOrdID, side, type, timeInForce, price,
    quantity, funds`. Captured verbatim from V.1 fixture.
    """

    model_config = _REQUEST_MODEL_CONFIG

    symbol_id: int = Field(alias="symbolID", ge=0)
    cl_ord_id: ClOrdID = Field(alias="clOrdID")
    side: OrderSide
    # NB: `type` shadows the Python builtin in this scope, but the JSON
    # key MUST be `type` to match the gateway. Pydantic field names can
    # shadow builtins; the only consequence is `Item.type` being shadowed
    # inside the class body, which we don't use.
    type: OrderType
    time_in_force: TimeInForce = Field(alias="timeInForce")
    price: DecimalString | None = Field(default=None)
    quantity: DecimalString | None = Field(default=None)
    funds: DecimalString | None = Field(default=None)


class SpotBatchNewOrderRequest(BaseModel):
    """Mirror of `BatchNewOrderRequest`. Field order: `accountID, orders`."""

    model_config = _REQUEST_MODEL_CONFIG

    account_id: int = Field(alias="accountID", ge=0)
    orders: list[SpotNewOrderItem] = Field(min_length=1)


class SpotCancelItem(BaseModel):
    """Mirror of `BatchCancelOrderItem`.

    Field order: `symbolID, clOrdID, orderID, origClOrdID`. Either
    `orderID` or `origClOrdID` identifies which order to cancel; both
    are optional in the schema and at least one must be set in practice
    (gateway-side validation — we don't enforce here to match Go SDK
    leniency).
    """

    model_config = _REQUEST_MODEL_CONFIG

    symbol_id: int = Field(alias="symbolID", ge=0)
    cl_ord_id: ClOrdID = Field(alias="clOrdID")
    order_id: int | None = Field(alias="orderID", default=None, ge=0)
    orig_cl_ord_id: ClOrdID | None = Field(alias="origClOrdID", default=None)


class SpotBatchCancelOrderRequest(BaseModel):
    """Mirror of `BatchCancelOrderRequest`. Field order: `accountID, cancels`."""

    model_config = _REQUEST_MODEL_CONFIG

    account_id: int = Field(alias="accountID", ge=0)
    cancels: list[SpotCancelItem] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Perps request schemas.
# ---------------------------------------------------------------------------


class PerpsOrderItem(BaseModel):
    """Mirror of `PerpsOrderItem` (perps/types).

    Field order is pinned by api.md §"Important rules" #2 verbatim:
    `clOrdID, modifier, side, type, timeInForce, price, quantity, funds,
    stopPrice, stopType, triggerType, reduceOnly, positionSide`.

    Required-with-zero-value invariant: `modifier`, `reduceOnly`, and
    `positionSide` must ALWAYS appear in the JSON even when their value
    is the zero/false equivalent (api.md §"Important rules" #4). This is
    enforced structurally — those fields have no default and are not
    Optional, so Pydantic refuses construction without them, and they
    don't carry `None` past `exclude_none=True` serialization.
    """

    model_config = _REQUEST_MODEL_CONFIG

    cl_ord_id: ClOrdID = Field(alias="clOrdID")
    modifier: OrderModifier
    side: OrderSide
    type: OrderType
    time_in_force: TimeInForce = Field(alias="timeInForce")
    price: DecimalString | None = Field(default=None)
    quantity: DecimalString | None = Field(default=None)
    funds: DecimalString | None = Field(default=None)
    stop_price: DecimalString | None = Field(alias="stopPrice", default=None)
    stop_type: StopType | None = Field(alias="stopType", default=None)
    trigger_type: TriggerType | None = Field(alias="triggerType", default=None)
    reduce_only: bool = Field(alias="reduceOnly")
    position_side: PositionSide = Field(alias="positionSide")


class PerpsNewOrderRequest(BaseModel):
    """Mirror of `PerpsNewOrderRequest`. Field order: `accountID, symbolID, orders`."""

    model_config = _REQUEST_MODEL_CONFIG

    account_id: int = Field(alias="accountID", ge=0)
    symbol_id: int = Field(alias="symbolID", ge=0)
    orders: list[PerpsOrderItem] = Field(min_length=1)


class PerpsCancelItem(BaseModel):
    """Mirror of `PerpsCancelItem`. Field order: `symbolID, orderID, clOrdID`.

    Same orderID/clOrdID dual-identifier pattern as the spot cancel item.
    """

    model_config = _REQUEST_MODEL_CONFIG

    symbol_id: int = Field(alias="symbolID", ge=0)
    order_id: int | None = Field(alias="orderID", default=None, ge=0)
    cl_ord_id: ClOrdID | None = Field(alias="clOrdID", default=None)


class PerpsCancelOrderRequest(BaseModel):
    """Mirror of `PerpsCancelOrderRequest`. Field order: `accountID, cancels`."""

    model_config = _REQUEST_MODEL_CONFIG

    account_id: int = Field(alias="accountID", ge=0)
    cancels: list[PerpsCancelItem] = Field(min_length=1)
