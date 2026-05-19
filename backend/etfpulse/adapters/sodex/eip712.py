"""EIP-712 typed-data builder.

Given a `payloadHash` (from `payload.py`) and a `nonce`, build the
EIP-712 typed-data dict the frontend will sign via wagmi/viem. The
dict shape matches the spec exactly:

    {
      types: {
        EIP712Domain: [...],
        ExchangeAction: [{payloadHash: bytes32}, {nonce: uint64}]
      },
      primaryType: "ExchangeAction",
      domain: {name, version, chainId, verifyingContract},
      message: {payloadHash, nonce}
    }

Per api.md §"Typed signature":
  - `domain.name`         — `"spot"` for spot actions, `"futures"` for perps.
  - `domain.version`      — always `"1"`.
  - `domain.chainId`      — api.md states a single value per environment
                            (mainnet `286623`, testnet `138565`). The V.1
                            fixture, however, captured `286623` for spot
                            and `138565` for perps. Whether SoDEX uses
                            per-venue chainIds OR our V.1 Go capture used
                            the wrong spot chainId is **not yet verified
                            against the live testnet** (V.2 endpoint probe
                            doesn't exercise signed writes). D.2 should
                            confirm with a signed-write smoke before
                            mainnet flip. The builder is venue-agnostic —
                            it hashes whatever chainId the caller passes.
  - `domain.verifyingContract` — always the zero address.

We emit `chainId` and `nonce` as Python `int`s (native EIP-712 spec
types — `uint256` and `uint64` respectively). The V.1 fixture stores
them as a hex string and decimal string respectively because Go's
`apitypes.TypedData.Domain.Map()` returns those representations — that's
an internal Go quirk, not a wire-format requirement. wagmi/viem's
`signTypedData` accepts native bigint/number for these types, and so
do the hashing libraries on both sides. Tests do semantic-equality
comparison, not strigified comparison.

This module imports only `typing` — no signing primitives. Anti-drift
rule 27 verified by absence.
"""

from __future__ import annotations

from typing import Any

# Per EIP-712 + api.md, this is the entire `verifyingContract` field —
# the SoDEX gateway intentionally uses the zero address (signature is
# verified off-chain by the gateway, not by a smart contract).
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Domain + ExchangeAction type definitions, stored as tuples of
# (name, type) PAIRS rather than dicts. Pairs are immutable; building
# the dict-form output requires a tiny allocation per call but
# eliminates the "module-level dict gets mutated by a caller" footgun.
# The previous design — `tuple[dict, ...]` — shared dict references
# across calls, so a caller doing `td["types"]["EIP712Domain"][0]["name"] = "X"`
# would poison every subsequent build.
_EIP712_DOMAIN_TYPE_PAIRS: tuple[tuple[str, str], ...] = (
    ("name", "string"),
    ("version", "string"),
    ("chainId", "uint256"),
    ("verifyingContract", "address"),
)

# ExchangeAction is the only primary type SoDEX uses for the
# `(payloadHash, nonce)` pair. All actions (newOrder, cancelOrder,
# updateLeverage, ...) hash their payload into the same envelope.
_EXCHANGE_ACTION_TYPE_PAIRS: tuple[tuple[str, str], ...] = (
    ("payloadHash", "bytes32"),
    ("nonce", "uint64"),
)


def _build_type_list(pairs: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    """Materialize the EIP-712 type-list from immutable (name, type) pairs.

    Each call returns a fresh list with fresh dicts so a caller mutating
    the returned typed-data cannot corrupt the next call's output.
    """
    return [{"name": name, "type": type_} for name, type_ in pairs]


def build_typed_data(
    *,
    domain_name: str,
    chain_id: int,
    payload_hash: str,
    nonce: int,
) -> dict[str, Any]:
    """Return the EIP-712 typed-data dict ready for wagmi/viem signing.

    Args:
        domain_name: `"spot"` for spot actions, `"futures"` for perps.
        chain_id: SoDEX chain ID — 138565/286623 testnet, 286623 mainnet
            (see api.md §"Endpoint"). Must be positive.
        payload_hash: 32-byte hex from `payload.py`. Must start with `0x`
            and be 66 chars total.
        nonce: Unix-millisecond nonce within the gateway's `(T-2d, T+1d)`
            window per api.md §"Sodex nonces". Caller is responsible for
            generation + tracking; this builder treats it opaquely.

    Returns:
        The full EIP-712 dict. Pass to the frontend as JSON and let
        wagmi/viem's `signTypedData` produce the 65-byte signature; the
        backend then prepends `0x01` to make it a SoDEX typed signature.

    Raises:
        ValueError: on malformed domain_name, chain_id, payload_hash, or
            nonce. Defensive — the schemas + payload builder upstream
            should already catch these, but a misuse from a future caller
            would fail loudly rather than producing a hash the gateway
            silently rejects.
    """
    if domain_name not in ("spot", "futures"):
        raise ValueError(f"domain_name must be 'spot' or 'futures', got {domain_name!r}")
    # `type(x) is int` rather than `isinstance(x, int)` — Python's bool
    # subclasses int, so `isinstance(True, int)` is True. A caller passing
    # `chain_id=True` (`==1`) or `nonce=False` (`==0`) would silently emit
    # JSON `{"chainId": true}` instead of `{"chainId": 1}`, which signs
    # a different EIP-712 message. Strict type check guards against this.
    if type(chain_id) is not int or chain_id <= 0:
        raise ValueError(f"chain_id must be a positive int, got {chain_id!r}")
    if (
        not isinstance(payload_hash, str)
        or not payload_hash.startswith("0x")
        or len(payload_hash) != 66
    ):
        raise ValueError(
            f"payload_hash must be '0x' + 64 hex chars (66 total), got {payload_hash!r}"
        )
    if type(nonce) is not int or nonce < 0:
        raise ValueError(f"nonce must be a non-negative int, got {nonce!r}")

    return {
        "types": {
            "EIP712Domain": _build_type_list(_EIP712_DOMAIN_TYPE_PAIRS),
            "ExchangeAction": _build_type_list(_EXCHANGE_ACTION_TYPE_PAIRS),
        },
        "primaryType": "ExchangeAction",
        "domain": {
            "name": domain_name,
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": ZERO_ADDRESS,
        },
        "message": {
            "payloadHash": payload_hash,
            "nonce": nonce,
        },
    }
