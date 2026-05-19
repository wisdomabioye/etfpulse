"""Tests for `etfpulse.adapters.sodex._http` — envelope parser, error
hierarchy, retry logic, weight estimator.

The HTTP core is the load-bearing piece for D.2: every read + write
flows through `SodexHttpClient._request`. Regressions in envelope
classification or retry behavior would silently flip auth errors into
validation errors, or worse, retry stable application failures and
burn rate-limit weight. These tests pin the contract.

We stub the network layer via httpx's transport plug — same pattern as
existing tests for the SoSoValue + Binance adapters, but for SoDEX the
fixture bytes come straight from V.2/V.3 captures so the parser is
exercised against real wire bytes.
"""

from __future__ import annotations

import json

import httpx
import pytest

from etfpulse.adapters.sodex._http import (
    SodexAuthError,
    SodexEnvelopeError,
    SodexError,
    SodexHttpClient,
    SodexHttpError,
    SodexParseError,
    SodexRateLimitError,
    SodexValidationError,
    estimate_request_weight,
)

# ---------------------------------------------------------------------------
# Weight estimator
# ---------------------------------------------------------------------------


class TestEstimateRequestWeight:
    """Static weight lookup per api-rate-limits.md. Used for observability
    logging only — doesn't gate calls. So our test bar is "produces a
    plausible value", not "matches gateway accounting exactly"."""

    def test_market_endpoint_weight_2(self):
        assert estimate_request_weight("GET", "/markets/symbols") == 2
        assert estimate_request_weight("GET", "/markets/bookTickers") == 2

    def test_account_state_weight_5(self):
        # Account paths include `{address}` in the middle — substring match
        # picks up the suffix regardless.
        assert estimate_request_weight("GET", "/accounts/0xabc.../state") == 5
        assert estimate_request_weight("GET", "/accounts/0xabc.../balances") == 5

    def test_write_endpoints_weight_1(self):
        """Batch writes are documented as `1 + N/40`; for our N=1 calls
        the table's base value of 1 is exact."""
        assert estimate_request_weight("POST", "/trade/orders/batch") == 1
        assert estimate_request_weight("DELETE", "/trade/orders/batch") == 1
        assert estimate_request_weight("POST", "/trade/orders") == 1

    def test_unknown_endpoint_falls_back_to_default(self):
        """Per api-rate-limits.md: 'Endpoints not listed below default to
        weight 20.' Catching that fallback explicitly so a typo or a
        truly-new endpoint produces a conservative estimate."""
        assert estimate_request_weight("GET", "/some-unknown-path") == 20

    def test_method_case_insensitive(self):
        """Internal API takes uppercased method, but defensive matching
        against `get`/`Get`/`GET` keeps the helper robust."""
        assert estimate_request_weight("get", "/markets/symbols") == 2
        assert estimate_request_weight("Post", "/trade/orders/batch") == 1


# ---------------------------------------------------------------------------
# Envelope-error classification
# ---------------------------------------------------------------------------


class TestEnvelopeErrorClassification:
    """Substring-match policy: auth-related messages → `SodexAuthError`,
    everything else → `SodexValidationError`. Verified phrases are
    sourced from V.3 captures (real testnet rejections)."""

    @pytest.mark.parametrize(
        "error_text",
        [
            "API key not found",
            "API key error: API key not found",
            "internal error: Failed to recover signer: Invalid recovery ID: bad recovery id",
            "signature mismatch",
            "Signature verification failed",
            "nonce out of window",
        ],
    )
    async def test_auth_substrings_route_to_auth_error(self, error_text: str, monkeypatch) -> None:
        client = _build_client(monkeypatch, body={"code": -1, "error": error_text})
        with pytest.raises(SodexAuthError) as exc_info:
            async with client:
                await client.get("/markets/symbols")
        assert exc_info.value.code == -1
        assert exc_info.value.raw_error == error_text

    async def test_unknown_error_routes_to_validation_error(self, monkeypatch) -> None:
        client = _build_client(monkeypatch, body={"code": -1, "error": "insufficient margin"})
        with pytest.raises(SodexValidationError) as exc_info:
            async with client:
                await client.get("/markets/symbols")
        assert exc_info.value.code == -1
        assert exc_info.value.raw_error == "insufficient margin"

    async def test_envelope_error_is_caught_by_base_class(self, monkeypatch) -> None:
        """Both auth and validation are `SodexEnvelopeError`s — and
        `SodexEnvelopeError` is a `SodexError`. Callers can catch broadly."""
        client = _build_client(monkeypatch, body={"code": -1, "error": "API key not found"})
        with pytest.raises(SodexEnvelopeError):
            async with client:
                await client.get("/markets/symbols")

        client2 = _build_client(monkeypatch, body={"code": -1, "error": "insufficient margin"})
        with pytest.raises(SodexError):
            async with client2:
                await client2.get("/markets/symbols")


# ---------------------------------------------------------------------------
# Success-envelope unwrapping
# ---------------------------------------------------------------------------


class TestSuccessEnvelope:
    async def test_code_zero_returns_data(self, monkeypatch) -> None:
        """The happy path: `code: 0` returns `data` directly. Verified
        shape from V.2 spot_account_state capture."""
        client = _build_client(
            monkeypatch,
            body={
                "code": 0,
                "timestamp": 1779139281482,
                "data": {"user": "0xabc", "aid": 57436, "uid": 57436, "B": []},
            },
        )
        async with client:
            data = await client.get("/accounts/0xabc/state")
        assert data == {"user": "0xabc", "aid": 57436, "uid": 57436, "B": []}

    async def test_code_zero_with_null_data_returns_none(self, monkeypatch) -> None:
        """Some V.2 read endpoints return `data: null` (when there are
        no matching rows). The envelope is still `code: 0`."""
        client = _build_client(monkeypatch, body={"code": 0, "data": None})
        async with client:
            data = await client.get("/accounts/0xabc/orders")
        assert data is None

    async def test_code_zero_with_array_data(self, monkeypatch) -> None:
        """Many endpoints return arrays — markets/symbols is the
        canonical example. The data field is `Any` so this round-trips."""
        client = _build_client(
            monkeypatch,
            body={"code": 0, "data": [{"id": 1, "name": "vETH_vUSDC"}]},
        )
        async with client:
            data = await client.get("/markets/symbols")
        assert data == [{"id": 1, "name": "vETH_vUSDC"}]


# ---------------------------------------------------------------------------
# Parse errors
# ---------------------------------------------------------------------------


class TestParseErrors:
    """Garbage responses must surface as `SodexParseError`, not be
    silently swallowed."""

    async def test_non_json_body_raises_parse_error(self, monkeypatch) -> None:
        client = _build_client(monkeypatch, raw_body=b"<html>cdn error</html>")
        with pytest.raises(SodexParseError, match="not JSON"):
            async with client:
                await client.get("/markets/symbols")

    async def test_json_non_object_raises_parse_error(self, monkeypatch) -> None:
        """JSON list at top level — not a SoDEX envelope."""
        client = _build_client(monkeypatch, raw_body=b"[1, 2, 3]")
        with pytest.raises(SodexParseError, match="not a JSON object"):
            async with client:
                await client.get("/markets/symbols")

    async def test_missing_code_raises_parse_error(self, monkeypatch) -> None:
        client = _build_client(monkeypatch, body={"data": {"x": 1}})
        with pytest.raises(SodexParseError, match="missing `code`"):
            async with client:
                await client.get("/markets/symbols")

    async def test_non_int_code_raises_parse_error(self, monkeypatch) -> None:
        client = _build_client(monkeypatch, body={"code": "0", "data": {}})
        with pytest.raises(SodexParseError, match="not an int"):
            async with client:
                await client.get("/markets/symbols")


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


class TestRetry:
    """5xx + 429 + network errors retry up to N times; envelope errors
    and 4xx-non-429 do NOT retry (deterministic application state).
    Tests use a sequenced transport that returns different responses on
    successive calls, so we can verify retry actually happens."""

    async def test_5xx_retries_then_succeeds(self, monkeypatch) -> None:
        """First call 503, second call 200/code:0 → returns data on
        the second attempt. Validates retry-on-5xx + recovery."""
        seq = _SequencedTransport(
            [
                _mock_response(503, b'{"code":-1,"error":"server down"}'),
                _mock_response(200, _json_bytes({"code": 0, "data": {"ok": True}})),
            ]
        )
        client = _build_client_with_transport(seq, retry_base_seconds=0.001)
        async with client:
            data = await client.get("/markets/symbols")
        assert data == {"ok": True}
        assert seq.call_count == 2

    async def test_5xx_exhausts_then_raises(self, monkeypatch) -> None:
        """All attempts return 503 → final raises `SodexHttpError` with
        the last status code. status_code attribute carries 503."""
        seq = _SequencedTransport([_mock_response(503, b"err") for _ in range(5)])
        client = _build_client_with_transport(seq, retry_max_attempts=3, retry_base_seconds=0.001)
        with pytest.raises(SodexHttpError) as exc_info:
            async with client:
                await client.get("/markets/symbols")
        assert exc_info.value.status_code == 503
        # Stops at retry_max_attempts (3), not the full 5 we queued.
        assert seq.call_count == 3

    async def test_429_raises_rate_limit_error(self, monkeypatch) -> None:
        seq = _SequencedTransport(
            [
                _mock_response(429, b'{"code":-1,"error":"rate limited"}'),
                _mock_response(429, b'{"code":-1,"error":"rate limited"}'),
                _mock_response(429, b'{"code":-1,"error":"rate limited"}'),
            ]
        )
        client = _build_client_with_transport(seq, retry_max_attempts=3, retry_base_seconds=0.001)
        with pytest.raises(SodexRateLimitError):
            async with client:
                await client.get("/markets/symbols")
        assert seq.call_count == 3

    async def test_envelope_error_does_not_retry(self, monkeypatch) -> None:
        """The most important non-retry case: `code: -1` is a
        deterministic application state. Retrying just burns weight.
        One call, then raise."""
        seq = _SequencedTransport(
            [
                _mock_response(200, _json_bytes({"code": -1, "error": "API key not found"})),
                _mock_response(200, _json_bytes({"code": 0, "data": {}})),  # never reached
            ]
        )
        client = _build_client_with_transport(seq, retry_base_seconds=0.001)
        with pytest.raises(SodexAuthError):
            async with client:
                await client.get("/markets/symbols")
        # Single call — proves we didn't retry through to the success.
        assert seq.call_count == 1

    async def test_4xx_non_429_does_not_retry(self, monkeypatch) -> None:
        """400 BadRequest = caller error. Retrying with the same payload
        repeats the failure. One call, then raise."""
        seq = _SequencedTransport(
            [
                _mock_response(400, b'{"code":-1,"error":"bad request"}'),
                _mock_response(200, _json_bytes({"code": 0, "data": {}})),  # never reached
            ]
        )
        client = _build_client_with_transport(seq, retry_base_seconds=0.001)
        # 4xx with parseable envelope routes through envelope classifier
        # (this case → validation error since 'bad request' has no auth
        # substring). The non-retry assertion holds either way.
        with pytest.raises(SodexEnvelopeError):
            async with client:
                await client.get("/markets/symbols")
        assert seq.call_count == 1

    async def test_4xx_with_envelope_code_zero_does_not_misclassify(self) -> None:
        """Edge case: HTTP 4xx with envelope `{code: 0, ...}` (wire contract
        violation — gateway shouldn't produce this) MUST raise
        `SodexHttpError`, NOT `SodexValidationError(code=0)`. The latter
        would be a misleading exception class for a 4xx with success-shape
        envelope. Added during the D.2 review pass."""
        seq = _SequencedTransport([_mock_response(403, _json_bytes({"code": 0, "data": {"x": 1}}))])
        client = _build_client_with_transport(seq, retry_base_seconds=0.001)
        with pytest.raises(SodexHttpError) as exc_info:
            async with client:
                await client.get("/markets/symbols")
        assert exc_info.value.status_code == 403
        # Crucially NOT SodexValidationError — `code=0` means success
        # envelope-wise, so the HTTP status is the operative signal.
        assert not isinstance(exc_info.value, SodexEnvelopeError)

    async def test_4xx_with_non_envelope_body_raises_http_error(self, monkeypatch) -> None:
        """A 4xx whose body isn't a SoDEX envelope (e.g. a Cloudfront
        error page) becomes `SodexHttpError(status_code=4xx)`."""
        seq = _SequencedTransport(
            [
                _mock_response(403, b"<html>Forbidden</html>"),
            ]
        )
        client = _build_client_with_transport(seq, retry_base_seconds=0.001)
        with pytest.raises(SodexHttpError) as exc_info:
            async with client:
                await client.get("/markets/symbols")
        assert exc_info.value.status_code == 403
        assert seq.call_count == 1

    async def test_network_error_retries(self, monkeypatch) -> None:
        """Connection refused / timeout / RemoteProtocolError → retry."""
        # Use a transport that raises httpx.ConnectError on first call,
        # then a normal response.
        from collections.abc import Iterator

        responses: Iterator[httpx.Response | Exception] = iter(
            [
                httpx.ConnectError("connection refused"),
                _mock_response(200, _json_bytes({"code": 0, "data": {"ok": True}})),
            ]
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            nxt = next(responses)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        transport = httpx.MockTransport(handler)
        client = _build_client_with_transport(transport, retry_base_seconds=0.001)
        async with client:
            data = await client.get("/markets/symbols")
        assert data == {"ok": True}


# ---------------------------------------------------------------------------
# POST + DELETE bodies
# ---------------------------------------------------------------------------


class TestPostAndDeleteBodies:
    """Writes carry a JSON body (the `params` object) and auth headers.
    The client passes them through verbatim — no signing, no
    transformation. Anti-drift rule 27."""

    async def test_post_sends_body_and_headers(self) -> None:
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["content"] = request.content
            captured["x_api_key"] = request.headers.get("X-API-Key")
            captured["x_api_sign"] = request.headers.get("X-API-Sign")
            captured["x_api_nonce"] = request.headers.get("X-API-Nonce")
            captured["content_type"] = request.headers.get("Content-Type")
            return _mock_response(
                200,
                _json_bytes({"code": 0, "data": [{"code": 0, "orderID": 42}]}),
            )

        client = _build_client_with_transport(httpx.MockTransport(handler))
        body = b'{"accountID":57436,"orders":[]}'
        headers = {
            "X-API-Key": "default",
            "X-API-Sign": "0x01abcdef",
            "X-API-Nonce": "1779222797167",
        }
        async with client:
            data = await client.post("/trade/orders/batch", body_bytes=body, headers=headers)

        assert data == [{"code": 0, "orderID": 42}]
        assert captured["method"] == "POST"
        assert captured["content"] == body  # exact byte-passthrough — no re-serialisation
        assert captured["x_api_key"] == "default"
        assert captured["x_api_sign"] == "0x01abcdef"
        assert captured["x_api_nonce"] == "1779222797167"
        # SoDEX gateway expects `Content-Type: application/json` per
        # rest-v1.md curl examples + V.3's working Go capture. httpx does
        # NOT auto-set this for `content=bytes`, so D.2's `_request`
        # method adds it centrally. Without this, the gateway may treat
        # the body as `application/octet-stream` and reject.
        assert captured["content_type"] == "application/json"

    async def test_post_caller_can_override_content_type(self) -> None:
        """If a caller explicitly passes Content-Type (case-insensitive
        match), the auto-add path respects the override rather than
        duplicating the header. Defensive — production callers don't
        override but a future endpoint with `application/protobuf` or
        similar should still work."""
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["content_type"] = request.headers.get("Content-Type")
            return _mock_response(200, _json_bytes({"code": 0, "data": []}))

        client = _build_client_with_transport(httpx.MockTransport(handler))
        async with client:
            await client.post(
                "/trade/orders/batch",
                body_bytes=b"x",
                headers={"content-type": "application/protobuf"},  # lowercase override
            )
        # Override respected — no double-add or `application/json`
        # squashing the explicit caller value.
        assert captured["content_type"] == "application/protobuf"

    async def test_delete_sends_body_and_headers(self) -> None:
        """Cancels are DELETE with a JSON body — uncommon REST shape
        but supported by httpx + SoDEX gateway."""
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["content"] = request.content
            return _mock_response(
                200,
                _json_bytes({"code": 0, "data": [{"code": 0}]}),
            )

        client = _build_client_with_transport(httpx.MockTransport(handler))
        body = b'{"accountID":57436,"cancels":[]}'
        async with client:
            await client.delete("/trade/orders/batch", body_bytes=body, headers={})

        assert captured["method"] == "DELETE"
        assert captured["content"] == body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_bytes(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _mock_response(status: int, body: bytes) -> httpx.Response:
    """Httpx Response with the given status + body. `request` is set
    by MockTransport at dispatch time, so we don't pre-populate it."""
    return httpx.Response(status_code=status, content=body)


def _build_client(monkeypatch, *, body=None, raw_body=None) -> SodexHttpClient:
    """Single-shot transport — every request returns the same body."""
    payload = raw_body if raw_body is not None else _json_bytes(body)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _mock_response(200, payload)

    transport = httpx.MockTransport(handler)
    return _build_client_with_transport(transport)


def _build_client_with_transport(
    transport: httpx.BaseTransport,
    *,
    retry_max_attempts: int = 3,
    retry_base_seconds: float = 0.001,
) -> SodexHttpClient:
    """Construct a client with the given transport. Speeds up tests by
    using millisecond backoff so retries don't add real wall time.

    Uses the explicit `transport` kwarg (added in the D.2 review pass)
    instead of patching `client._client._transport` — the latter is a
    private httpx attribute that future versions could rename."""
    return SodexHttpClient(
        base_url="https://example.test/api/v1/spot",
        timeout_seconds=5.0,
        retry_max_attempts=retry_max_attempts,
        retry_base_seconds=retry_base_seconds,
        transport=transport,
    )


class _SequencedTransport(httpx.AsyncBaseTransport):
    """AsyncBaseTransport that returns a sequence of pre-built responses
    on successive calls. Used for retry tests where attempt N needs a
    different response. `httpx.AsyncBaseTransport` is the right base
    class — `BaseTransport` is the sync variant and the async client
    needs `aclose()` for shutdown."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.call_count >= len(self._responses):
            raise AssertionError(
                f"Sequenced transport exhausted: {self.call_count + 1} calls "
                f"made, only {len(self._responses)} queued"
            )
        resp = self._responses[self.call_count]
        self.call_count += 1
        return resp

    async def aclose(self) -> None:
        # No real resources to release — the responses are pre-built.
        return None
