"""SoDEX HTTP client core — envelope-aware async client, error hierarchy,
weight-aware request logging.

Public surface
--------------
This module is INTERNAL to `etfpulse.adapters.sodex` (leading underscore on
filename). The venue clients (`spot_client.py`, `perps_client.py`) import
`SodexHttpClient` + the error classes; everything else stays private to
the package.

The wire contract — fully pinned by V.2 + V.3 captures
------------------------------------------------------
SoDEX uses HTTP 200 for ALL responses (success AND application errors).
The success-vs-error distinction lives in the response BODY envelope:

    {"code": <int>, "timestamp": <unix-ms>, "data": <object|array|null>,
     "error": <str?>, "message": <str?>}

Envelope semantics, verified against the live testnet:

  - `code == 0`         → success. `data` carries the parsed result.
  - `code != 0`         → failure. `error` (preferred) or `message` carries
                          the human-readable cause. `data` is omitted.
  - Auth failures        → envelope `code` is `-1` with messages like
                          "API key not found", "API key error: ...",
                          "Failed to recover signer: ...". HTTP status is
                          still 200.
  - Validation failures → also envelope `code != 0`. The per-order writes
                          have a TWO-LEVEL envelope (outer `code: 0` +
                          inner `data: [{code, error?, orderID?}]`); we
                          surface the outer code here, the venue clients
                          unpack the inner `ResponseData` array.

This module's job:
  1. Parse the envelope from EVERY response (success or error).
  2. Translate envelope failures into typed exceptions so callers can
     pattern-match (e.g. catch `SodexAuthError` separately from
     `SodexValidationError`).
  3. Retry on transient failure (HTTP 429, 5xx, network errors).
  4. Log every request with endpoint weight for observability.

Anti-drift rule 27 (CLAUDE.md) — this module MUST NOT import any signing
primitive (`eth_account.*`, `web3.auto.signing`, `sign_typed_data`, etc.).
The HTTP client RECEIVES already-signed requests; it never produces them.
The grep test in `tests/test_adapters/test_sodex_typed_data.py` enforces
this for every `.py` under `etfpulse/adapters/sodex/`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class SodexError(Exception):
    """Base for every SoDEX adapter failure. Callers that want to log
    'something went wrong with SoDEX' but don't care which axis catch
    this; callers that need pattern-match (auth vs validation vs
    transient) catch the more specific subclasses below."""


class SodexHttpError(SodexError):
    """A network-layer or non-2xx (excluding 429) HTTP failure that wasn't
    retried or whose retries were exhausted. Carries `status_code` (None
    for network errors that never got a response)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SodexRateLimitError(SodexError):
    """HTTP 429 or an envelope error explicitly indicating rate-limit
    rejection. Distinct from `SodexHttpError` so callers can apply
    rate-limit-specific backoff (per-address limits are stricter than
    IP-weight per api-rate-limits.md)."""


class SodexEnvelopeError(SodexError):
    """HTTP 200 with envelope `code != 0`. Catch this for any
    application-level failure. Specialised subclasses below give more
    granular pattern-match for the cases we've actually observed."""

    def __init__(self, message: str, *, code: int, raw_error: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw_error = raw_error


class SodexAuthError(SodexEnvelopeError):
    """Auth-related envelope failure. Verified messages from V.3:
      - "API key not found"
      - "API key error: API key not found"
      - "internal error: Failed to recover signer: Invalid recovery ID: ..."
    The substring-match policy mirrors SoSoValue's 429 classifier:
    catching a known stable phrase is safe-by-construction — if SoDEX
    ever reworks wording, the worst case is one error being misrouted to
    `SodexValidationError` instead, which is still distinguishable from
    success."""


class SodexValidationError(SodexEnvelopeError):
    """Catch-all for envelope failures that aren't auth-related. Includes
    parameter validation, balance checks ("insufficient margin"), state
    errors ("order rejected: OrderNotFound"), etc. The raw error string
    is preserved on `.raw_error` for diagnostic logging."""


class SodexParseError(SodexError):
    """Response body wasn't valid JSON or didn't have the expected
    envelope shape. Indicates either a SoDEX gateway bug, a non-SoDEX
    URL accidentally hit (e.g. CDN error page), or a wire-contract drift
    that V.2/V.3 didn't catch. Treated as non-retryable — retrying the
    same call against the same buggy response wastes weight."""


# ---------------------------------------------------------------------------
# Envelope-error classification
# ---------------------------------------------------------------------------

# Substrings that classify an envelope error as auth-related vs other.
# Each substring is matched against the lowercased `error`/`message` field.
# Order matters only insofar as the first hit wins (we return on first
# match in `_classify_envelope_error`).
#
# This list is SAFE-BY-CONSTRUCTION: the substrings are stable phrases
# observed in V.3 captures. If SoDEX changes wording, the worst case is
# the error reroutes to `SodexValidationError` — still distinguishable
# from success, still loggable, still surfaceable to the operator.
_AUTH_SUBSTRINGS = (
    "api key not found",
    "api key error",
    "failed to recover signer",
    "invalid recovery id",
    "signature mismatch",
    "signature verification failed",
    # Nonce-window violations from api.md §"Sodex nonces" (T-2d, T+1d).
    # Specific phrases rather than bare "nonce" — the latter would
    # over-match on validation errors like "nonce field is required".
    "nonce out of window",
    "nonce already used",
    "nonce too low",
    "nonce too high",
)


def _classify_envelope_error(code: int, error_text: str) -> SodexEnvelopeError:
    """Choose the most specific exception class for an envelope error."""
    lower = error_text.lower()
    for needle in _AUTH_SUBSTRINGS:
        if needle in lower:
            return SodexAuthError(error_text, code=code, raw_error=error_text)
    return SodexValidationError(error_text, code=code, raw_error=error_text)


# ---------------------------------------------------------------------------
# Endpoint weight table — per api-rate-limits.md
# ---------------------------------------------------------------------------

# Each key is a (METHOD, path_suffix_pattern) tuple — the suffix matches
# against the URL path AFTER the venue base. Static weights only; dynamic
# weights (orderbook depth, batch size N, history items) are documented
# in api-rate-limits.md but D.2 doesn't use those endpoints yet — when
# they're added, extend this table with a callable instead of an int.
#
# A missing entry falls back to `_DEFAULT_WEIGHT` (20) per the docs:
#   > "Endpoints not listed below default to weight 20."
#
# Suffix matching is plain substring (no regex) because the suffixes are
# unique. The order of insertion doesn't matter — we iterate the dict
# in order but each entry is selected by exact suffix membership.
_DEFAULT_WEIGHT = 20

_WEIGHT_TABLE: dict[tuple[str, str], int] = {
    # Spot + perps market endpoints (full literal paths — no placeholders).
    ("GET", "/markets/symbols"): 2,
    ("GET", "/markets/coins"): 2,
    ("GET", "/markets/tickers"): 2,
    ("GET", "/markets/miniTickers"): 2,
    ("GET", "/markets/bookTickers"): 2,
    # Perps additionally has mark-prices.
    ("GET", "/markets/mark-prices"): 2,
    # Account endpoints — paths are `/accounts/{address}/<suffix>`. We
    # match by SUFFIX since `{address}` varies. These suffixes are
    # unique across the API surface in (method, suffix) space.
    ("GET", "/balances"): 5,
    ("GET", "/orders"): 5,  # account open orders (GET); /trade/orders is POST/DELETE
    ("GET", "/positions"): 5,
    ("GET", "/state"): 5,
    ("GET", "/api-keys"): 5,
    ("GET", "/fee-rate"): 2,
    # Trading writes — base weight; batch writes scale `1 + N/40` but
    # D.2 callers send N=1 (single-order batches), so 1 is accurate.
    # The full paths are matched here so `/trade/orders/batch` doesn't
    # accidentally get classified as `/orders` (longest-match handles it).
    ("POST", "/trade/orders/batch"): 1,
    ("DELETE", "/trade/orders/batch"): 1,
    ("POST", "/trade/orders"): 1,  # perps new
    ("DELETE", "/trade/orders"): 1,  # perps cancel
    ("POST", "/trade/orders/replace"): 1,
    ("POST", "/trade/orders/schedule-cancel"): 1,
    ("POST", "/trade/leverage"): 1,
    ("POST", "/trade/margin"): 1,
    ("POST", "/accounts/transfers"): 10,
}


def estimate_request_weight(method: str, path: str) -> int:
    """Best-effort static weight estimate for a request. Returns the
    documented default (20) if the path doesn't match any known entry.

    Matching is longest-suffix wins so `/trade/orders/batch` resolves to
    the batch entry (weight 1) rather than the bare `/orders` entry
    (which exists for account-orders reads). Substring match would be
    too permissive — a literal `endswith` ensures the URL really ends
    with the documented suffix."""
    method_upper = method.upper()
    matches = [
        (suffix, weight)
        for (m, suffix), weight in _WEIGHT_TABLE.items()
        if m == method_upper and path.endswith(suffix)
    ]
    if not matches:
        return _DEFAULT_WEIGHT
    matches.sort(key=lambda x: len(x[0]), reverse=True)
    return matches[0][1]


# ---------------------------------------------------------------------------
# Envelope parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Envelope:
    """The shape of every SoDEX response body (success or error).

    `data` is `Any` because SoDEX returns objects, arrays, and (rarely)
    null depending on the endpoint. The venue client's Pydantic DTO is
    responsible for narrowing.
    """

    code: int
    timestamp: int | None
    data: Any
    error: str | None


def _parse_envelope(body_bytes: bytes, *, method: str, path: str) -> _Envelope:
    """Parse the response body or raise `SodexParseError`. Does NOT raise
    on `code != 0` — that's the caller's job once the envelope is
    extracted (see `_extract_data`)."""
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError as exc:
        raise SodexParseError(f"SoDEX response was not JSON ({method} {path}): {exc}") from exc
    if not isinstance(body, dict):
        raise SodexParseError(
            f"SoDEX response was not a JSON object ({method} {path}): got {type(body).__name__}"
        )
    if "code" not in body:
        raise SodexParseError(
            f"SoDEX response missing `code` field ({method} {path}): {body!r:.200}"
        )
    code = body["code"]
    # `bool` is a subclass of `int` in Python — `isinstance(True, int)`
    # is True. Use `type(...) is int` so a bool `code` is rejected
    # cleanly rather than treated as 0/1 (cf. D.1's chain_id guard).
    if type(code) is not int:
        raise SodexParseError(
            f"SoDEX response `code` was not an int ({method} {path}): {type(code).__name__}"
        )
    # `error` is preferred on write paths; `message` appears on some
    # read-path errors. Both are documented (in api.md) as the same
    # human-readable cause string. We surface whichever is set.
    error_text = body.get("error") or body.get("message")
    raw_timestamp = body.get("timestamp")
    return _Envelope(
        code=code,
        timestamp=raw_timestamp if isinstance(raw_timestamp, int) else None,
        data=body.get("data"),
        error=error_text if isinstance(error_text, str) else None,
    )


def _extract_data(env: _Envelope) -> Any:
    """Raise the right error if `code != 0`; otherwise return `data`."""
    if env.code == 0:
        return env.data
    raise _classify_envelope_error(env.code, env.error or f"code={env.code}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SodexHttpClient:
    """Async HTTP client for one SoDEX venue (spot or perps).

    The venue clients (`SodexSpotClient`, `SodexPerpsClient`) own one
    instance each. The lifecycle is explicit:

        async with SodexHttpClient(base_url=...) as client:
            data = await client.get("/markets/symbols")

    or manual:

        client = SodexHttpClient(base_url=...)
        try:
            data = await client.get(...)
        finally:
            await client.close()

    `get`/`post`/`delete` return the unwrapped `envelope.data` on success
    or raise the appropriate `SodexError` subclass. They DO NOT return
    the full envelope — the timestamp is logged but not surfaced because
    no caller has needed it; if we later want it, expose `_request_raw`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        retry_max_attempts: int,
        retry_base_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Construct the client.

        `transport` is an explicit injection point for tests — pass a
        `httpx.MockTransport` (or any `AsyncBaseTransport`) to stub the
        network layer. Production code never passes it; the default
        `None` means httpx uses its real network transport.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._retry_max = retry_max_attempts
        self._retry_base = retry_base_seconds
        # `transport=None` is the documented httpx default — passing it
        # explicitly when None is set causes no behavior change but
        # keeps the construction path uniform.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            # Reasonable connection pool — at our volume we open at most
            # a handful of concurrent requests per venue. Defaults
            # (10 max keepalive, 100 max total) are vastly more than
            # we'll use; we set explicit limits to make this conscious.
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def __aenter__(self) -> SodexHttpClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        await self._client.aclose()

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request("GET", path, params=params, headers=None, body_bytes=None)

    async def post(
        self,
        path: str,
        *,
        body_bytes: bytes,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Submit a signed write. `body_bytes` is the EXACT bytes the
        wallet signed (the `params` object — no `{type, params}` wrapper
        for the HTTP body per api.md L111). `headers` MUST include
        X-API-Key (key name, not address), X-API-Sign, X-API-Nonce."""
        return await self._request(
            "POST", path, params=None, headers=headers, body_bytes=body_bytes
        )

    async def delete(
        self,
        path: str,
        *,
        body_bytes: bytes,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """DELETE with a JSON body — used for batch cancels. SoDEX's
        cancel endpoints are DELETE methods that carry the cancel-item
        list in the body, same signing semantics as POST."""
        return await self._request(
            "DELETE", path, params=None, headers=headers, body_bytes=body_bytes
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        body_bytes: bytes | None,
    ) -> Any:
        """Execute a request with retry on transient failures.

        Retry policy:
          - HTTP 429 → retry with exponential backoff. Final attempt
            failure → `SodexRateLimitError`.
          - HTTP 5xx → retry. Final attempt failure → `SodexHttpError`
            carrying the last status code.
          - Network errors / timeouts → retry. Final attempt failure →
            `SodexHttpError(status_code=None)`.
          - HTTP 2xx → parse envelope. Envelope `code == 0` → return
            data. Envelope `code != 0` → classify + raise (NO retry —
            envelope errors are application-level, not transient).
          - HTTP 4xx (non-429) → `SodexHttpError` immediately (no retry).
          - Body parse failure → `SodexParseError` (no retry).
        """
        weight = estimate_request_weight(method, path)
        # Auto-add `Content-Type: application/json` when sending a body.
        # The SoDEX gateway expects it (curl examples in rest-v1.md set
        # it explicitly; V.3's Go capture also set it — that's why V.3
        # signed writes worked). httpx does NOT set Content-Type
        # automatically for `content=bytes`; the caller has to. We do
        # it here, centrally, so individual venue clients can't forget.
        # If the caller pre-set Content-Type in `headers`, we respect
        # the override (case-insensitive lookup since HTTP headers are).
        if body_bytes is not None:
            merged_headers = dict(headers or {})
            has_content_type = any(k.lower() == "content-type" for k in merged_headers)
            if not has_content_type:
                merged_headers["Content-Type"] = "application/json"
            headers = merged_headers
        last_exc: Exception | None = None
        for attempt in range(1, self._retry_max + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    headers=headers,
                    content=body_bytes,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                log.warning(
                    "sodex_request_network_error",
                    method=method,
                    path=path,
                    weight=weight,
                    attempt=attempt,
                    max_attempts=self._retry_max,
                    error=str(exc),
                )
                if attempt >= self._retry_max:
                    raise SodexHttpError(
                        f"network error after {attempt} attempts: {exc}",
                        status_code=None,
                    ) from exc
                await self._sleep_backoff(attempt)
                continue

            status = response.status_code
            # 5xx: server error, retry.
            if 500 <= status < 600:
                log.warning(
                    "sodex_request_server_error",
                    method=method,
                    path=path,
                    weight=weight,
                    status=status,
                    attempt=attempt,
                    max_attempts=self._retry_max,
                )
                if attempt >= self._retry_max:
                    raise SodexHttpError(
                        f"server error {status} after {attempt} attempts on {method} {path}",
                        status_code=status,
                    )
                await self._sleep_backoff(attempt)
                continue
            # 429: rate-limited, retry.
            if status == 429:
                log.warning(
                    "sodex_request_rate_limited",
                    method=method,
                    path=path,
                    weight=weight,
                    attempt=attempt,
                    max_attempts=self._retry_max,
                )
                if attempt >= self._retry_max:
                    raise SodexRateLimitError(
                        f"rate-limited after {attempt} attempts on {method} {path}",
                    )
                await self._sleep_backoff(attempt)
                continue
            # 4xx non-429: client error, do NOT retry — re-attempting the
            # same call with the same body will fail the same way.
            if 400 <= status < 500:
                # Try to surface envelope-style error body if present;
                # else use plain status. A 4xx that DOES carry a SoDEX
                # envelope (some gateway error paths do) reveals more.
                #
                # Guard: only classify-as-envelope when the envelope
                # itself indicates failure (`code != 0`). A 4xx response
                # with envelope `code: 0` would be a wire contract
                # violation; treating it as an envelope error would
                # raise `SodexValidationError(code=0)` which is
                # misleading. Fall through to plain `SodexHttpError`
                # in that case.
                try:
                    env = _parse_envelope(response.content, method=method, path=path)
                    if env.code != 0:
                        log.error(
                            "sodex_request_client_error",
                            method=method,
                            path=path,
                            weight=weight,
                            status=status,
                            envelope_code=env.code,
                            envelope_error=env.error,
                        )
                        raise _classify_envelope_error(env.code, env.error or f"http {status}")
                    # 4xx with envelope code=0 — fall through to plain
                    # HttpError below for honesty.
                    log.error(
                        "sodex_request_client_error_inconsistent_envelope",
                        method=method,
                        path=path,
                        weight=weight,
                        status=status,
                        envelope_code=env.code,
                    )
                    raise SodexHttpError(
                        f"client error {status} on {method} {path} "
                        f"(envelope claimed code=0 — wire contract violation)",
                        status_code=status,
                    ) from None
                except SodexParseError:
                    log.error(
                        "sodex_request_client_error",
                        method=method,
                        path=path,
                        weight=weight,
                        status=status,
                        body_preview=response.content[:200].decode("utf-8", errors="replace"),
                    )
                    raise SodexHttpError(
                        f"client error {status} on {method} {path}",
                        status_code=status,
                    ) from None

            # 2xx: parse envelope and either return data or raise an
            # application-level error. Envelope errors are NOT retried —
            # they reflect deterministic application state (wrong nonce,
            # wrong key, insufficient balance) that won't change on retry.
            #
            # `response.elapsed` would be useful telemetry but is only
            # populated reliably with a real network transport; the
            # httpx MockTransport used in tests doesn't set it without
            # an explicit `.aread()` first, and we don't need that
            # round-trip just for a log field. Real prod traffic will
            # have httpx's own request_log middleware available for
            # latency tracking if we want to enable it later.
            env = _parse_envelope(response.content, method=method, path=path)
            log.info(
                "sodex_request_ok",
                method=method,
                path=path,
                weight=weight,
                status=status,
                envelope_code=env.code,
            )
            return _extract_data(env)

        # Unreachable — every loop branch either returns, raises, or
        # continues. This `raise` exists only to satisfy type-checkers
        # that the function always either returns or raises.
        raise SodexHttpError(  # pragma: no cover
            f"exhausted retries unexpectedly on {method} {path}",
            status_code=None,
        ) from last_exc

    async def _sleep_backoff(self, attempt: int) -> None:
        """Exponential backoff: `base * 2^(attempt-1)` seconds.
        attempt=1 → base*1, attempt=2 → base*2, attempt=3 → base*4, ..."""
        delay = self._retry_base * (2 ** (attempt - 1))
        await asyncio.sleep(delay)
