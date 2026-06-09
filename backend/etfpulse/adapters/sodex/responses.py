"""Pydantic mirrors of SoDEX response shapes.

Each model mirrors the `data` payload of a specific endpoint — the
outer envelope `{code, timestamp, data}` is stripped by
`_http.SodexHttpClient` before the DTO is constructed.

Authoritative shape source — ordered by precedence:

  1. `tests/fixtures/sodex_endpoint_responses.json` (V.2) — 20 read
     endpoints captured against live testnet. Byte-exact ground truth
     for reads.
  2. `tests/fixtures/sodex_signed_write_responses.json` (V.3) — write
     endpoints captured against live testnet (perps succeeded,
     spot rejected with envelope error). Byte-exact ground truth for
     writes' two-level envelope shape.
  3. `docs/sodex/sodex.com/documentation/api/rest-v1/schema.md` — when
     fixtures don't cover an edge case (e.g., a field that's always
     null in our captures but documented optional).

Design choices
--------------
  - `extra="ignore"` on every model. SoDEX may add fields without
    breaking us. The two-way contract is: we know the fields we read,
    we don't care about new ones.
  - Pydantic v2 + `Field(alias=...)` for camelCase ↔ snake_case
    mapping. Internal Python code uses snake_case; aliases match the
    SoDEX wire shape.
  - `populate_by_name=True` so tests + callers can construct models
    via either casing.
  - Decimal-as-string fields use `str` in the DTO. SoDEX emits them
    as quoted JSON strings (api.md §"Important rules" #3); converting
    to Decimal at the venue-client boundary loses precision-control
    information. Callers that need Decimal can wrap explicitly.
  - Nullable fields use `T | None` with `default=None`. Many
    endpoints return `null` for empty arrays (e.g. `O: null` when
    no open orders) — this is documented and we model it faithfully.

Anti-drift rule 27 (CLAUDE.md): this module imports only `pydantic` —
no signing primitives. Verified by the existing grep test.
"""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared model config — applied to every DTO.
# ---------------------------------------------------------------------------

# `extra="ignore"`: silently drop unknown fields. Forward-compatible by
# design — SoDEX adding `newFancyField` shouldn't break our parser.
# `populate_by_name=True`: accept either snake_case or camelCase at
# construction. Production calls use the alias (wire shape); tests
# benefit from constructing via the Pythonic name.
_RESPONSE_MODEL_CONFIG = ConfigDict(
    extra="ignore",
    populate_by_name=True,
)


# ---------------------------------------------------------------------------
# Market endpoints (spot + perps share most field shapes)
# ---------------------------------------------------------------------------


class SpotSymbol(BaseModel):
    """One entry in `GET /spot/markets/symbols → data`. Captured 32
    entries on testnet — every venue trading pair the gateway exposes.
    All Decimal-typed fields stay as `str` (api.md #3)."""

    model_config = _RESPONSE_MODEL_CONFIG

    id: int
    name: str
    display_name: str = Field(alias="displayName")
    base_coin_id: int = Field(alias="baseCoinID")
    base_coin: str = Field(alias="baseCoin")
    base_coin_precision: int = Field(alias="baseCoinPrecision")
    quote_coin_id: int = Field(alias="quoteCoinID")
    quote_coin: str = Field(alias="quoteCoin")
    quote_coin_precision: int = Field(alias="quoteCoinPrecision")
    price_precision: int = Field(alias="pricePrecision")
    tick_size: str = Field(alias="tickSize")
    min_price: str = Field(alias="minPrice")
    max_price: str = Field(alias="maxPrice")
    quantity_precision: int = Field(alias="quantityPrecision")
    step_size: str = Field(alias="stepSize")
    min_quantity: str = Field(alias="minQuantity")
    max_quantity: str = Field(alias="maxQuantity")
    market_min_quantity: str = Field(alias="marketMinQuantity")
    market_max_quantity: str = Field(alias="marketMaxQuantity")
    min_notional: str = Field(alias="minNotional")
    max_notional: str = Field(alias="maxNotional")
    buy_limit_up_ratio: str = Field(alias="buyLimitUpRatio")
    sell_limit_down_ratio: str = Field(alias="sellLimitDownRatio")
    market_deviation_ratio: str = Field(alias="marketDeviationRatio")
    maker_fee: str = Field(alias="makerFee")
    taker_fee: str = Field(alias="takerFee")
    status: str  # "TRADING" observed; future statuses possible


class PerpsSymbol(BaseModel):
    """One entry in `GET /perps/markets/symbols → data`. Has more
    fields than spot (leverage / funding-rate / open-interest), but
    shares the price/lot/notional filter shape. `marginTiers` is a
    nested array we surface as `list[dict]` because we don't parse it
    today (D.3 risk controller may want a sub-DTO)."""

    model_config = _RESPONSE_MODEL_CONFIG

    id: int
    name: str
    display_name: str = Field(alias="displayName")
    base_coin: str = Field(alias="baseCoin")
    quote_coin_id: int = Field(alias="quoteCoinID")
    quote_coin: str = Field(alias="quoteCoin")
    quote_coin_precision: int = Field(alias="quoteCoinPrecision")
    price_precision: int = Field(alias="pricePrecision")
    tick_size: str = Field(alias="tickSize")
    min_price: str = Field(alias="minPrice")
    max_price: str = Field(alias="maxPrice")
    quantity_precision: int = Field(alias="quantityPrecision")
    step_size: str = Field(alias="stepSize")
    min_quantity: str = Field(alias="minQuantity")
    max_quantity: str = Field(alias="maxQuantity")
    market_min_quantity: str = Field(alias="marketMinQuantity")
    market_max_quantity: str = Field(alias="marketMaxQuantity")
    min_notional: str = Field(alias="minNotional")
    max_notional: str = Field(alias="maxNotional")
    buy_limit_up_ratio: str = Field(alias="buyLimitUpRatio")
    sell_limit_down_ratio: str = Field(alias="sellLimitDownRatio")
    market_deviation_ratio: str = Field(alias="marketDeviationRatio")
    maker_fee: str = Field(alias="makerFee")
    taker_fee: str = Field(alias="takerFee")
    status: str
    # Perps-specific
    init_leverage: int = Field(alias="initLeverage")
    max_leverage: int = Field(alias="maxLeverage")
    funding_interval: int = Field(alias="fundingInterval")
    min_funding_rate: str = Field(alias="minFundingRate")
    max_funding_rate: str = Field(alias="maxFundingRate")
    interest_rate: str = Field(alias="interestRate")
    open_interest_cap: str = Field(alias="openInterestCap")
    open_interest_cap_usd: str = Field(alias="openInterestCapUSD")
    margin_tiers: list[dict] = Field(alias="marginTiers", default_factory=list)


class Coin(BaseModel):
    """`GET /{venue}/markets/coins → data[i]`. Spot returns minimal
    `{id, name, precision}`; perps adds `marginRatio` + `price`. We
    use one model with the perps-only fields optional so spot DTOs
    don't need to lie about absent fields."""

    model_config = _RESPONSE_MODEL_CONFIG

    id: int
    name: str
    precision: int
    # Perps-only — omitted on spot.
    margin_ratio: str | None = Field(alias="marginRatio", default=None)
    price: str | None = None


class Ticker(BaseModel):
    """`GET /{venue}/markets/tickers → data[i]`. Perps adds funding /
    open-interest / mark-price fields; spot omits them. Single model
    with perps fields optional follows the same pattern as `Coin`."""

    model_config = _RESPONSE_MODEL_CONFIG

    symbol: str
    last_px: str = Field(alias="lastPx")
    open_px: str = Field(alias="openPx")
    high_px: str = Field(alias="highPx")
    low_px: str = Field(alias="lowPx")
    bid_px: str = Field(alias="bidPx")
    bid_sz: str = Field(alias="bidSz")
    ask_px: str = Field(alias="askPx")
    ask_sz: str = Field(alias="askSz")
    volume: str
    quote_volume: str = Field(alias="quoteVolume")
    change: str
    # `changePct` arrives as a JSON NUMBER (int 0 when flat, float
    # otherwise) — verified via V.2 captures across both venues. This
    # is inconsistent with `change` which is always a quoted-string
    # decimal, but mirrors the gateway's actual wire shape.
    change_pct: float | int | str = Field(alias="changePct")
    open_time: int = Field(alias="openTime")
    close_time: int = Field(alias="closeTime")
    # Perps-only
    funding_rate: str | None = Field(alias="fundingRate", default=None)
    next_funding_time: int | None = Field(alias="nextFundingTime", default=None)
    mark_price: str | None = Field(alias="markPrice", default=None)
    index_price: str | None = Field(alias="indexPrice", default=None)
    open_interest: str | None = Field(alias="openInterest", default=None)


class MiniTicker(BaseModel):
    """`GET /{venue}/markets/miniTickers → data[i]`. Spot-only in our
    V.2 capture (perps tickers carry the same data). Subset of
    `Ticker` — same fields minus bid/ask/change."""

    model_config = _RESPONSE_MODEL_CONFIG

    symbol: str
    last_px: str = Field(alias="lastPx")
    open_px: str = Field(alias="openPx")
    high_px: str = Field(alias="highPx")
    low_px: str = Field(alias="lowPx")
    volume: str
    quote_volume: str = Field(alias="quoteVolume")
    open_time: int = Field(alias="openTime")
    close_time: int = Field(alias="closeTime")


class BookTicker(BaseModel):
    """`GET /{venue}/markets/bookTickers → data[i]`. Top-of-book only.
    Cheaper to query than full order book; useful for spread tracking."""

    model_config = _RESPONSE_MODEL_CONFIG

    symbol: str
    bid_px: str = Field(alias="bidPx")
    bid_sz: str = Field(alias="bidSz")
    ask_px: str = Field(alias="askPx")
    ask_sz: str = Field(alias="askSz")


class PerpsMarkPrice(BaseModel):
    """`GET /perps/markets/mark-prices → data[i]`. Perps-only.
    Subset of the ticker fields focused on the mark / funding pair —
    what risk engines actually need for margin calc."""

    model_config = _RESPONSE_MODEL_CONFIG

    symbol: str
    mark_price: str = Field(alias="markPrice")
    index_price: str = Field(alias="indexPrice")
    funding_rate: str = Field(alias="fundingRate")
    next_funding_time: int = Field(alias="nextFundingTime")
    open_interest: str = Field(alias="openInterest")


# ---------------------------------------------------------------------------
# Account endpoints
# ---------------------------------------------------------------------------


class BalanceEntry(BaseModel):
    """One row in a balances array. SoDEX returns TWO key conventions:
    `/accounts/{addr}/balances` uses LONG keys (`id`, `coin`, `total`,
    `locked` — confirmed against a funded testnet wallet), while the
    compact `/state.B` shape uses SHORT keys (`i`, `a`, `t`, `l`). The
    V.3 capture's balances array was EMPTY (unfunded burner), so the
    short-key assumption was never exercised — the funded-wallet 500 in
    `account-summary` surfaced the long-key reality.

    `AliasChoices` accepts BOTH forms per field so a single model serves
    both endpoints. `asset` carries the venue coin name verbatim (e.g.
    `vUSDC`); the route maps it to the display symbol downstream."""

    model_config = _RESPONSE_MODEL_CONFIG

    instrument_id: int = Field(validation_alias=AliasChoices("i", "id"))
    asset: str = Field(validation_alias=AliasChoices("a", "coin"))
    total: str = Field(validation_alias=AliasChoices("t", "total"))
    locked: str = Field(validation_alias=AliasChoices("l", "locked"))


class AccountBalances(BaseModel):
    """`GET /{venue}/accounts/{addr}/balances → data`. Wraps the
    balance array with block-meta from when the snapshot was taken."""

    model_config = _RESPONSE_MODEL_CONFIG

    balances: list[BalanceEntry] = Field(default_factory=list)
    block_height: int = Field(alias="blockHeight")
    block_time: int = Field(alias="blockTime")


class SpotAccountState(BaseModel):
    """`GET /spot/accounts/{addr}/state → data`. Compact shape used by
    the frontend for quick state polling. Short keys verbatim from
    the wire format.

    `B` (balances), `O` (open orders) may be `null` when empty —
    SoDEX uses null-instead-of-empty-array on this endpoint."""

    model_config = _RESPONSE_MODEL_CONFIG

    user: str
    aid: int
    uid: int
    balances: list[BalanceEntry] | None = Field(alias="B", default=None)
    # Open orders — DTO TBD when we encounter a non-null capture; for
    # now, accept any list shape.
    orders: list[dict] | None = Field(alias="O", default=None)


class PerpsAccountState(BaseModel):
    """`GET /perps/accounts/{addr}/state → data`. Perps adds margin
    fields `av/am/ami/amw/im/cm/oim/ocm` (all `str` — Decimal-shaped),
    plus `P` (positions) and `S` (sub-positions or settlements; we
    don't know which yet, exposed as `list[dict]`).

    Short field names verbatim from wire format. Documented mapping:
      - av  = account value
      - am  = account margin
      - ami = ami (initial margin?)
      - amw = amw (margin warning?)
      - im  = initial margin
      - cm  = current margin
      - oim = open interest margin
      - ocm = open contract margin
    These are educated guesses — the precise semantics aren't documented
    in rest-v1/schema.md and V.3's perps account had all zeros so we
    couldn't reverse-engineer from values. D.3 will need clarification
    from SoDEX or a non-zero capture before risk-controller code reads
    them; until then we surface them as raw strings without semantic
    interpretation."""

    model_config = _RESPONSE_MODEL_CONFIG

    user: str
    aid: int
    uid: int
    account_value: str = Field(alias="av")
    account_margin: str = Field(alias="am")
    ami: str
    amw: str
    initial_margin: str = Field(alias="im")
    current_margin: str = Field(alias="cm")
    oim: str
    ocm: str
    balances: list[BalanceEntry] | None = Field(alias="B", default=None)
    positions: list[dict] | None = Field(alias="P", default=None)
    orders: list[dict] | None = Field(alias="O", default=None)
    sub_state: list[dict] | None = Field(alias="S", default=None)


class OpenOrdersResponse(BaseModel):
    """`GET /{venue}/accounts/{addr}/orders → data`. Spot + perps
    share the wrapper shape (`orders` array + `blockHeight` + `blockTime`).
    Order item shape itself isn't fully captured in V.2 (the burner had
    no open orders); exposed as `list[dict]` until we have a non-empty
    capture. D.4 or D.5 will likely add a typed item model."""

    model_config = _RESPONSE_MODEL_CONFIG

    orders: list[dict] = Field(default_factory=list)
    block_height: int = Field(alias="blockHeight")
    block_time: int = Field(alias="blockTime")


class OpenPositionsResponse(BaseModel):
    """`GET /perps/accounts/{addr}/positions → data`. Note: the V.2
    capture's response has key `orders` despite the endpoint being
    /positions — that's a SoDEX gateway naming quirk (likely shared
    response struct on the Go side). We preserve the wire shape and
    expose the field as `positions` via alias so Python callers don't
    inherit the confusion."""

    model_config = _RESPONSE_MODEL_CONFIG

    # Wire alias `orders` (yes, really, on /positions). Field is named
    # `positions` so the misleading wire name doesn't leak.
    positions: list[dict] = Field(alias="orders", default_factory=list)
    block_height: int = Field(alias="blockHeight")
    block_time: int = Field(alias="blockTime")


class FeeRate(BaseModel):
    """`GET /{venue}/accounts/{addr}/fee-rate → data`. Static per-tier
    structure. Same shape on spot + perps."""

    model_config = _RESPONSE_MODEL_CONFIG

    fee_tier: int = Field(alias="feeTier")
    maker_fee_rate: str = Field(alias="makerFeeRate")
    taker_fee_rate: str = Field(alias="takerFeeRate")
    maker_rebate_tier: int = Field(alias="makerRebateTier")
    staking_tier: int = Field(alias="stakingTier")


class APIKey(BaseModel):
    """`GET /{venue}/accounts/{addr}/api-keys → data[i]`. Per
    api.md §"API keys", these are the named EVM signing keys the
    gateway uses to look up signers on signed-write requests. The
    `name` field is what goes in the `X-API-Key` header (NOT the
    publicKey — that misled V.3's first capture). `expiresAt: 0`
    means never expires."""

    model_config = _RESPONSE_MODEL_CONFIG

    name: str
    type: str  # "EVM" — the only documented value today
    public_key: str = Field(alias="publicKey")
    expires_at: int = Field(alias="expiresAt")


# ---------------------------------------------------------------------------
# Trading endpoints (writes)
# ---------------------------------------------------------------------------


class OrderResponseItem(BaseModel):
    """One entry in `POST /{venue}/trade/orders[/batch] → data[i]`.
    The dual-layer envelope contract:

      - Outer envelope: `{code: 0, timestamp, data: [...]}` — verified
        gateway accepted the request as a whole.
      - Each `data[i]` carries its own `code`:
          - `code: 0`  → the order was accepted into the matching engine.
                         `orderID` is populated. `error` is null/absent.
          - `code: !=0`→ the order was rejected at validation OR matching.
                         `error` carries the reason. `orderID` is absent.

    The same shape is used for both `POST` (new) and `DELETE` (cancel)
    — captured both in V.3.

    Per-order error examples from V.3:
      - "insufficient margin"
      - "order rejected: OrderNotFound" (cancel non-existent order)

    api.md §"Place multiple orders" >> "ResponseData": shape is `{code,
    clOrdID, error?, orderID?}`. `clOrdID` is documented as `true` (always
    populated) BUT V.3 perps cancel response shows `clOrdID` absent when
    the inner code is `-1` — gateway behavior is inconsistent with docs
    here. We mark it Optional to match observed reality."""

    model_config = _RESPONSE_MODEL_CONFIG

    code: int
    cl_ord_id: str | None = Field(alias="clOrdID", default=None)
    order_id: int | None = Field(alias="orderID", default=None)
    error: str | None = None


class TransferResponse(BaseModel):
    """`POST /{venue}/accounts/transfers → data`. Not captured in V.3
    (transfer-asset signed action is out of D.2 scope) — shape sourced
    from rest-v1.md §"Transfer asset". Surfaced for completeness so
    D.3/D.4 risk controllers have a typed handle when transfers land."""

    model_config = _RESPONSE_MODEL_CONFIG

    transfer_id: int = Field(alias="transferID", default=0)
    status: str = ""
