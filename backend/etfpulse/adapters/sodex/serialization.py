r"""Compact-JSON serializer that mirrors Go's `json.Marshal` byte-for-byte.

This is one of the two load-bearing modules for SoDEX EIP-712 signing.
The gateway re-derives `payloadHash` from the request body via Go's
`json.Marshal` and then verifies the wallet's signature against that
hash. If our compact JSON differs from Go's output by ANY byte —
whitespace, key order, escaping, decimal representation — the hash
diverges, the signature fails to verify, and the order is rejected.

The four invariants we mirror:

1. **No whitespace.** `json.dumps(..., separators=(",", ":"))` matches
   Go's default (no spaces after `:` or `,`).
2. **Pydantic field declaration order preserved.** `sort_keys=False` (the
   default, but we set it explicitly so a future refactor can't silently
   re-enable sorting). Pydantic v2's `model_dump` already preserves
   declaration order, which we set to match the Go SDK struct field
   order in `schemas.py`.
3. **`omitempty` semantics.** `exclude_none=True` drops fields whose
   value is `None` — matching Go's `omitempty` tag behavior. Required
   fields are non-Optional in the schema, so they always serialize even
   when their value is the zero/false equivalent.
4. **Aliases applied.** `by_alias=True` emits camelCase keys
   (`accountID`, `clOrdID`) instead of the snake_case Python field
   names.

We return `str` (UTF-8 text), not `bytes`, because the V.1 fixture
stores `payload_json` as a string. Step 3 (`payload.py`) calls
`.encode("utf-8")` before keccak256.

Go-vs-Python byte-divergence on string content:

1. **HTML escape (`<`, `>`, `&`).** Go's `json.Marshal` escapes these
   by default (the `EscapeHTML` option); Python's `json.dumps` does
   not. Today's schema — enum literals, ASCII decimal strings, ASCII
   alphanumeric clOrdIDs, hex blobs — contains none of these chars,
   so the outputs converge. If a free-form string field is ever
   added (memo, label, etc.), add an HTML-escape post-pass here.

2. **Non-ASCII characters.** Go's `json.Marshal` emits raw UTF-8 bytes;
   Python's `json.dumps` with `ensure_ascii=True` would escape them as
   `\uXXXX`. Those are DIFFERENT byte sequences and would silently
   break the byte-exact contract. We set `ensure_ascii=False` to match
   Go. Today's schema can't admit non-ASCII anyway — every user-supplied
   string (DecimalString, ClOrdID, payloadHash, verifyingContract,
   domain.name) is regex- or enum-constrained to ASCII. The flag is
   defense-in-depth against a future free-form field landing without
   the author noticing the encoding divergence.

3. **NaN / Infinity.** Go's `json.Marshal` returns an error when asked
   to encode a NaN or Infinity float (the JSON spec disallows them);
   Python's `json.dumps` defaults to `allow_nan=True` and silently
   emits the non-spec literals `NaN`, `Infinity`, `-Infinity`. Those
   bytes would never round-trip through Go's `json.Unmarshal` on the
   gateway side. We set `allow_nan=False` so Python raises `ValueError`
   instead of producing a body the gateway will reject silently
   (failing loudly here is strictly better — the request never leaves
   our process). Today's schema has no float fields (DecimalString is
   `str`, integers are typed `int`, enums are `IntEnum`), so this is
   unreachable; the flag pins the contract for any future float field.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def compact_json(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    """Serialize a Pydantic model or pre-built dict/list to compact JSON.

    For a `BaseModel`, applies `by_alias=True` (emit camelCase) +
    `exclude_none=True` (omitempty). For a raw dict/list, serializes
    verbatim — the caller is responsible for having already emitted
    correct field order.
    """
    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(by_alias=True, exclude_none=True)
    else:
        payload = value
    return json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=False,
        ensure_ascii=False,
        allow_nan=False,
    )
