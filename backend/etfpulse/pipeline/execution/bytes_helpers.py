"""Byte-exact extraction of the HTTP body from a stored EIP-712 payload.

The wallet signs the FULL D.1 `payload_json` —
    {"type":"newOrder","params":<INNER>}
— and we persist those exact bytes on `Order.eip712_payload` (TEXT, not
JSONB — see anti-drift rule 31). The HTTP body the D.2 client sends is
the `<INNER>` value ONLY, per D.2's wire-contract finding ("HTTP body
is `params` only — no `{type, params}` wrapper. Same field order /
serialisation as the signed payload because the gateway re-hashes the
body to verify the signature").

This module's job: extract those `<INNER>` bytes from the stored payload
WITHOUT round-tripping through `json.loads` + `json.dumps`. The Python
JSON library makes no byte-stability guarantee (whitespace, numeric
formatting, key order can drift). Any divergence from what the wallet
signed makes the gateway reject the submission. We must emit the EXACT
bytes the signature was computed against — pure string slicing
guarantees that.

Validation: we still `json.loads` to verify the stored payload is
well-formed `{type, params}`; on malformed input we raise loudly
rather than emitting garbage bytes to the gateway.
"""

from __future__ import annotations

import json

_TYPE_PARAMS_MARKER = ',"params":'
_EXPECTED_TOP_KEYS = {"type", "params"}


def extract_params_bytes(payload_json: str) -> bytes:
    """Return the byte-exact `params` value from a D.1 `payload_json`.

    The structural shape is pinned by `adapters/sodex/payload.py`:
    `{"type":"<ACTION>","params":<INNER>}`. The `,"params":` marker
    appears EXACTLY ONCE in a well-formed payload — at the boundary
    between the type's closing quote and the inner value. Our action-
    type values (`newOrder`, `cancelOrder`) are simple ASCII strings;
    Pydantic-constrained inner fields (DecimalString regex, ClOrdID
    regex, IntEnum ints, BigInt account/symbol IDs) cannot embed the
    literal `,"params":` substring.

    Returns the `<INNER>` value as UTF-8 bytes, suitable to feed as
    `body_bytes` to `SodexSpotClient.submit_batch_new_order` /
    `submit_batch_cancel_order` (and perps equivalents).

    Raises:
        ValueError if payload_json is not valid JSON, doesn't have the
        expected `{type, params}` top-level shape, doesn't contain the
        structural marker exactly once, or doesn't end with `}`.
    """
    # Validate structurally (without using the parsed result). The
    # parse is for "is this well-formed" only — the bytes we emit are
    # the original string slice, not the round-tripped JSON.
    try:
        parsed = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"payload_json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"payload_json must be a JSON object; got {type(parsed).__name__}")
    if set(parsed.keys()) != _EXPECTED_TOP_KEYS:
        raise ValueError(
            f"payload_json must have keys {sorted(_EXPECTED_TOP_KEYS)}; got {sorted(parsed.keys())}"
        )

    # Byte-exact extraction.
    occurrences = payload_json.count(_TYPE_PARAMS_MARKER)
    if occurrences != 1:
        raise ValueError(
            f"payload_json structural marker {_TYPE_PARAMS_MARKER!r} appears "
            f"{occurrences} times (expected exactly 1)"
        )
    if not payload_json.endswith("}"):
        raise ValueError("payload_json must end with `}`")

    start = payload_json.index(_TYPE_PARAMS_MARKER) + len(_TYPE_PARAMS_MARKER)
    return payload_json[start:-1].encode("utf-8")
