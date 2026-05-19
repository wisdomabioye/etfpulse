"""Payload-hash builder. The second half of the byte-exact compatibility
contract with the SoDEX gateway.

The gateway computes `payloadHash` as:

    payloadHash = keccak256(json.Marshal({"type": "<action>", "params": <params>}))

Two subtleties this module nails down:

1. **Two-step concatenation** mirrors the Go SDK's actual implementation
   (verified in `scripts/sodex_verify/eip712_capture/main.go`). We
   compact-serialize the params first, then string-concatenate the
   `{"type":...,"params":` prefix and the closing brace around it.
   Doing `json.dumps({"type": ..., "params": params_dict})` instead
   would re-serialize the inner dict and risk losing the field order
   we worked so hard to pin in `schemas.py` + `serialization.py`.

2. **`keccak256` not SHA3-256.** The two algorithms differ by exactly
   one byte in the padding constant. EIP-712 uses keccak256 (the
   pre-standardization variant). `eth_utils.keccak` is the right
   primitive. **Anti-drift rule 27 (CLAUDE.md):** this module must
   only import hash primitives, NEVER signing primitives. `eth_utils`
   provides `keccak`; that's allowed. Importing from `eth_account`,
   `web3.auto.signing`, or any module that ships private-key handling
   is prohibited.

Return shape: a dataclass with `payload_json` (the bytes the gateway
re-hashes) + `payload_hash` (the 0x-prefixed 32-byte hex). Both are
captured so reconciliation pipelines can show "what the wallet signed"
without re-deriving from other columns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from eth_utils import keccak
from pydantic import BaseModel

from etfpulse.adapters.sodex.serialization import compact_json


@dataclass(frozen=True, slots=True)
class PayloadResult:
    """Output of `build_payload`: the compact JSON + its keccak256 hash."""

    payload_json: str
    payload_hash: str  # "0x" + 64 lowercase hex chars


def build_payload(action_type: str, params: BaseModel | dict[str, Any]) -> PayloadResult:
    """Build `{"type": <action>, "params": <params>}` and hash it.

    `action_type` is one of `"newOrder"`, `"cancelOrder"`, `"transferAsset"`,
    `"scheduleCancel"`, `"revokeAPIKey"`, `"updateLeverage"`, `"updateMargin"`.
    See api.md §"How to compute payloadHash" for the full list.

    `params` is either a Pydantic request model (preferred) or a pre-built
    dict (rare, mostly for tests).

    Two-step concat note: serializing the inner params first and string-
    concatenating around it preserves the exact byte sequence the Go SDK
    produces. Building a single outer dict and json.dumps-ing would
    silently re-order fields if Pydantic ever changes its dump order.
    """
    inner = compact_json(params)
    # `ensure_ascii=False` + `allow_nan=False` mirror `compact_json`'s policy —
    # both halves of `payload_json` must share the same encoding/spec
    # discipline to stay byte-exact with Go's `json.Marshal` (raw UTF-8,
    # error on NaN). Today's action_types are all ASCII string literals
    # so both flags are functionally moot; pinned for consistency.
    action_quoted = json.dumps(action_type, ensure_ascii=False, allow_nan=False)
    payload_json = f'{{"type":{action_quoted},"params":{inner}}}'
    payload_hash = "0x" + keccak(payload_json.encode("utf-8")).hex()
    return PayloadResult(payload_json=payload_json, payload_hash=payload_hash)
