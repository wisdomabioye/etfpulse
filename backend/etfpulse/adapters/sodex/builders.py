"""Public composer functions — the API surface of the SoDEX EIP-712 layer.

Four functions, one per (venue, action) pair:

  - `build_spot_new_order`     — venue=sodex_spot,  action=newOrder
  - `build_spot_cancel_order`  — venue=sodex_spot,  action=cancelOrder
  - `build_perps_new_order`    — venue=sodex_perps, action=newOrder
  - `build_perps_cancel_order` — venue=sodex_perps, action=cancelOrder

Each takes a validated Pydantic request + the chain_id + nonce, and
returns a `TypedDataBundle` containing everything D.4 (the API route)
needs to:
  1. Persist on the `Order` row (`eip712_payload`, `nonce`, `account_id`,
     `symbol_id`, etc).
  2. Hand back to the frontend (`typed_data`) for wagmi/viem signing.
  3. Later submit the signed payload + `eip712_signature` to the
     gateway and record `payload_hash` for reconciliation.

Domain-name vs venue:

| Label                      | "sodex_spot"          | "sodex_perps"         |
|----------------------------|-----------------------|-----------------------|
| EIP-712 `domain.name`      | `"spot"`              | `"futures"`           |
| Our `Order.venue` enum     | `"sodex_spot"`        | `"sodex_perps"`       |

The bundle stores BOTH the SoDEX protocol-side `domain.name` (implicit
via `typed_data`) and our internal `venue` string so reconciliation
queries can filter by venue without parsing the typed-data dict.

Chain ID is a caller responsibility. api.md §"Endpoint" states a single
chainId per environment (mainnet `286623`, testnet `138565`). The V.1
fixture captured `286623` for spot and `138565` for perps — whether
that reflects a real per-venue split, an environment difference between
the two captures, or a Go-capture quirk is **not yet verified against
the live testnet** (the V.2 endpoint probe doesn't exercise signed
writes). D.2 should confirm with a signed-write smoke before mainnet
flip. Until then, D.4 will source per-venue chainIds from env vars so
operators can override without code changes. The composers here are
venue-agnostic — they pass through whatever chainId the caller resolved.

Nonce is also a caller responsibility. Per api.md §"Sodex nonces",
nonces are Unix milliseconds within `(T-2d, T+1d)`, tracked per API
key (= per wallet address). D.4 will own per-wallet nonce generation;
this layer treats nonces opaquely.

This module imports ONLY from `etfpulse.adapters.sodex.*` — never from
`eth_account` or any signing primitive. Anti-drift rule 27 (CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from etfpulse.adapters.sodex.eip712 import build_typed_data
from etfpulse.adapters.sodex.payload import build_payload
from etfpulse.adapters.sodex.schemas import (
    PerpsCancelOrderRequest,
    PerpsNewOrderRequest,
    SpotBatchCancelOrderRequest,
    SpotBatchNewOrderRequest,
)

# Domain names from api.md §"Typed signature". "futures" (not "perps")
# is what SoDEX uses for the perps EIP-712 domain — the venue label
# `sodex_perps` is our internal taxonomy, kept distinct on purpose.
_SPOT_DOMAIN_NAME = "spot"
_PERPS_DOMAIN_NAME = "futures"

# Internal venue identifiers (match Order.venue CHECK constraint added
# in PR C.6).
_SPOT_VENUE = "sodex_spot"
_PERPS_VENUE = "sodex_perps"

# Action identifiers — verbatim strings the gateway expects in the
# `{type, params}` payload wrapper. See api.md §"How to compute payloadHash".
_ACTION_NEW_ORDER = "newOrder"
_ACTION_CANCEL_ORDER = "cancelOrder"


@dataclass(frozen=True, slots=True)
class TypedDataBundle:
    """Result of any of the four composer functions.

    Frozen so a caller can't mutate `payload_json` after it's been
    hashed — that would break the "the wallet signs what we stored"
    invariant. `typed_data` is the only non-string field; we don't
    freeze the dict inside, but Python convention (and our anti-drift
    discipline) treats it as immutable post-construction.

    Field mapping for D.4's INSERT path:
      payload_json     → Order.eip712_payload — MUST be persisted as TEXT (or
                         equivalent byte-preserving column), NOT round-tripped
                         through `json.loads` into JSONB. JSONB normalizes
                         whitespace, may reorder keys, and can re-quote
                         numerics — any of which silently breaks the byte-
                         exact reconciliation contract (the gateway re-hashes
                         the body it received; we must store what the wallet
                         signed verbatim). If JSONB-queryability is needed
                         later, cast on read (`eip712_payload::jsonb`) or
                         add a separate JSONB projection column.
      payload_hash     → Order.eip712_payload_hash (persist alongside payload
                         for O(1) reconciliation lookups; also derivable on
                         demand from payload_json by re-hashing).
      typed_data       → returned to frontend, NOT persisted (rebuildable
                         from payload_hash + chain_id + venue mapping).
      action_type      → Order.action_type (or folded into a status/intent
                         field — D.4's call).
      venue            → Order.venue.
      chain_id         → kept in memory / env-driven per-venue; not persisted.
      nonce            → Order.nonce.
    """

    payload_json: str  # the bytes the gateway re-hashes — store on Order row
    payload_hash: str  # "0x" + 64 hex; derivable, kept for log/debug
    typed_data: dict[str, Any]  # full EIP-712 dict; pass to wagmi/viem
    action_type: str  # "newOrder" | "cancelOrder"
    venue: str  # "sodex_spot" | "sodex_perps"
    chain_id: int
    nonce: int


# ---------------------------------------------------------------------------
# Composers
# ---------------------------------------------------------------------------


def build_spot_new_order(
    *,
    request: SpotBatchNewOrderRequest,
    chain_id: int,
    nonce: int,
) -> TypedDataBundle:
    """Build a SoDEX spot `newOrder` typed-data bundle.

    Args:
        request: a validated `SpotBatchNewOrderRequest` carrying accountID
            + one or more `SpotNewOrderItem`s.
        chain_id: SoDEX chainId for the spot environment. Caller resolves
            from env. See module docstring — the per-venue split observed
            in V.1 is not yet confirmed against the live testnet.
        nonce: Unix-millisecond nonce. Must be unique per (wallet, action)
            within the gateway's `(T-2d, T+1d)` window. D.4 owns generation.
    """
    return _build(
        request=request,
        action_type=_ACTION_NEW_ORDER,
        domain_name=_SPOT_DOMAIN_NAME,
        venue=_SPOT_VENUE,
        chain_id=chain_id,
        nonce=nonce,
    )


def build_spot_cancel_order(
    *,
    request: SpotBatchCancelOrderRequest,
    chain_id: int,
    nonce: int,
) -> TypedDataBundle:
    """Build a SoDEX spot `cancelOrder` typed-data bundle.

    Mirrors `build_spot_new_order` but with `cancelOrder` action.
    """
    return _build(
        request=request,
        action_type=_ACTION_CANCEL_ORDER,
        domain_name=_SPOT_DOMAIN_NAME,
        venue=_SPOT_VENUE,
        chain_id=chain_id,
        nonce=nonce,
    )


def build_perps_new_order(
    *,
    request: PerpsNewOrderRequest,
    chain_id: int,
    nonce: int,
) -> TypedDataBundle:
    """Build a SoDEX perps `newOrder` typed-data bundle.

    Args:
        chain_id: SoDEX chainId for the perps environment. Caller resolves
            from env. See module docstring — the per-venue split observed
            in V.1 is not yet confirmed against the live testnet.
    """
    return _build(
        request=request,
        action_type=_ACTION_NEW_ORDER,
        domain_name=_PERPS_DOMAIN_NAME,
        venue=_PERPS_VENUE,
        chain_id=chain_id,
        nonce=nonce,
    )


def build_perps_cancel_order(
    *,
    request: PerpsCancelOrderRequest,
    chain_id: int,
    nonce: int,
) -> TypedDataBundle:
    """Build a SoDEX perps `cancelOrder` typed-data bundle."""
    return _build(
        request=request,
        action_type=_ACTION_CANCEL_ORDER,
        domain_name=_PERPS_DOMAIN_NAME,
        venue=_PERPS_VENUE,
        chain_id=chain_id,
        nonce=nonce,
    )


# ---------------------------------------------------------------------------
# Internal helper — the four composers all share this shape.
# ---------------------------------------------------------------------------


def _build(
    *,
    request: SpotBatchNewOrderRequest
    | SpotBatchCancelOrderRequest
    | PerpsNewOrderRequest
    | PerpsCancelOrderRequest,
    action_type: str,
    domain_name: str,
    venue: str,
    chain_id: int,
    nonce: int,
) -> TypedDataBundle:
    payload = build_payload(action_type, request)
    typed_data = build_typed_data(
        domain_name=domain_name,
        chain_id=chain_id,
        payload_hash=payload.payload_hash,
        nonce=nonce,
    )
    return TypedDataBundle(
        payload_json=payload.payload_json,
        payload_hash=payload.payload_hash,
        typed_data=typed_data,
        action_type=action_type,
        venue=venue,
        chain_id=chain_id,
        nonce=nonce,
    )
