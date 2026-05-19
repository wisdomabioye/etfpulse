"""Shared helpers used by both venue clients (`spot_client.py` +
`perps_client.py`).

These were originally defined inside `spot_client.py` with leading
underscores ("private to spot"), but `perps_client.py` needed them
too — having one venue's module own helpers the other imports is a
real code smell that confuses module ownership. Moved here so each
venue client imports from a clearly-shared module.

Leading underscore on the filename keeps these private to the
`etfpulse.adapters.sodex` package (mirrors `_http.py` convention).
Functions themselves don't carry an underscore prefix because they
ARE the package's intentional internal API surface — D.3/D.4 will
not import them, but the venue clients legitimately should.

Anti-drift rule 27: no signing primitives imported.
"""

from __future__ import annotations

from typing import Any

from etfpulse.adapters.sodex._http import SodexParseError
from etfpulse.adapters.sodex.responses import OrderResponseItem


def signed_write_headers(
    *,
    api_key_name: str,
    typed_signature: str,
    nonce: int,
) -> dict[str, str]:
    """Build the auth-header trio for a signed write.

    Headers are case-sensitive on the SoDEX gateway: `X-API-Key`,
    `X-API-Sign`, `X-API-Nonce` (NOT `x-api-key` etc). Exact casing
    is V.3-verified.

    Note: `Content-Type: application/json` is NOT set here — the HTTP
    core's `_request` method adds it automatically when `body_bytes`
    is non-None. Keeping that responsibility centralised means the
    venue clients never have to remember to include it.
    """
    return {
        "X-API-Key": api_key_name,
        "X-API-Sign": typed_signature,
        "X-API-Nonce": str(nonce),
    }


def lower_address(address: str) -> str:
    """Normalise an EVM address to lowercase with `0x` prefix.

    V.3 verified the gateway is case-sensitive on the api-keys lookup
    against the registered `publicKey` (always stored lowercase). URL
    paths accept any case on reads, but we normalise everywhere for
    consistency — easier to grep for, fewer surprise mismatches.
    Idempotent if already lowercase + prefixed.
    """
    out = address.lower()
    return out if out.startswith("0x") else f"0x{out}"


def validate_order_response_items(data: Any) -> list[OrderResponseItem]:
    """Validate the `data` field of a write response is the expected
    list of per-order items.

    Per V.3 capture, successful write envelopes carry `data` as a list
    of `{code, clOrdID?, orderID?, error?}` dicts. We've NOT observed
    `data: null` on a write success — V.3 didn't cover that case. The
    `SodexParseError` here is a defensive guard: if the gateway ever
    DOES return `code:0, data: null` on a write, we surface it loudly
    rather than silently returning an empty list (which the caller
    might mistake for "no orders accepted").
    """
    if not isinstance(data, list):
        raise SodexParseError(
            f"expected list[OrderResponseItem] from trade write, got {type(data).__name__}"
        )
    return [OrderResponseItem.model_validate(row) for row in data]
