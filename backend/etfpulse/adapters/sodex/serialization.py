"""Compact-JSON serializer that mirrors Go's `json.Marshal` byte-for-byte.

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

What Go's `json.Marshal` does that Python does NOT do, by default:

- Go HTML-escapes `<`, `>`, `&` in JSON strings (the `EscapeHTML` option,
  on by default). Our data domain — enum literals, ASCII decimal strings,
  alphanumeric clOrdIDs — never contains these characters, so the two
  outputs converge on every value we'd ever emit. Documented here so a
  future case involving free-form strings (e.g. a user-supplied memo
  field) is treated with care. Add an HTML-escape post-pass if the
  schema ever admits such input.
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
        ensure_ascii=True,
    )
