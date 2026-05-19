"""Replay tests for `SodexSpotClient` — stubs httpx with V.2 read +
V.3 write capture bytes and asserts the client method returns the
expected typed DTO.

This is the integration-style layer of D.2's test pyramid: the HTTP
core, DTOs, and method routing are all exercised at once against
real wire bytes. If any layer drifts, this tier fails — the more
focused tests in `test_sodex_http.py` + `test_sodex_responses.py`
isolate which.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from etfpulse.adapters.sodex._helpers import (
    lower_address,
    signed_write_headers,
)
from etfpulse.adapters.sodex._http import (
    SodexAuthError,
    SodexHttpClient,
    SodexParseError,
)
from etfpulse.adapters.sodex.spot_client import SodexSpotClient

_BURNER = "0xcaba55de67f421bddb0813961a20988885aa98d7"

_V2_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sodex_endpoint_responses.json"
)
_V3_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sodex_signed_write_responses.json"
)


def _load_probes(path: Path) -> dict[str, object]:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return {p["name"]: p for p in d["probes"]}


V2 = _load_probes(_V2_PATH)
V3 = _load_probes(_V3_PATH)


# ---------------------------------------------------------------------------
# Helpers: build a client whose transport always returns a specific captured
# response. The path the client requests isn't actually used by the
# transport — it returns the same bytes either way — but we still pass
# the documented URL for log/debug clarity.
# ---------------------------------------------------------------------------


def _build_client_returning(response_body: object) -> SodexSpotClient:
    """Build a SodexSpotClient whose HTTP transport returns the given
    JSON-encoded body bytes on every request. Status 200."""

    payload = json.dumps(response_body).encode("utf-8")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, content=payload)

    transport = httpx.MockTransport(handler)
    http = SodexHttpClient(
        base_url="https://example.test/api/v1/spot",
        timeout_seconds=5.0,
        retry_max_attempts=1,
        retry_base_seconds=0.001,
        transport=transport,
    )
    return SodexSpotClient(http)


def _build_client_capturing_request(
    response_body: object,
) -> tuple[SodexSpotClient, dict]:
    """Like _build_client_returning, but also records the request that
    came through so write-path tests can verify headers + body."""

    captured: dict = {}
    payload = json.dumps(response_body).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["content"] = bytes(request.content)
        return httpx.Response(status_code=200, content=payload)

    transport = httpx.MockTransport(handler)
    http = SodexHttpClient(
        base_url="https://example.test/api/v1/spot",
        timeout_seconds=5.0,
        retry_max_attempts=1,
        retry_base_seconds=0.001,
        transport=transport,
    )
    return SodexSpotClient(http), captured


# ---------------------------------------------------------------------------
# Market reads
# ---------------------------------------------------------------------------


class TestMarketReads:
    async def test_get_symbols_returns_typed_list(self):
        client = _build_client_returning(V2["spot_markets_symbols"]["response"])
        async with client:
            symbols = await client.get_symbols()
        assert len(symbols) > 0
        veth = next(s for s in symbols if s.name == "vETH_vUSDC")
        assert veth.id == 2

    async def test_get_coins_returns_typed_list(self):
        client = _build_client_returning(V2["spot_markets_coins"]["response"])
        async with client:
            coins = await client.get_coins()
        assert len(coins) > 0
        # Spot coins have no margin_ratio
        assert all(c.margin_ratio is None for c in coins)

    async def test_get_tickers_returns_typed_list(self):
        client = _build_client_returning(V2["spot_markets_tickers"]["response"])
        async with client:
            tickers = await client.get_tickers()
        assert len(tickers) > 0
        assert all(t.symbol for t in tickers)

    async def test_get_mini_tickers_returns_typed_list(self):
        client = _build_client_returning(V2["spot_markets_mini_tickers"]["response"])
        async with client:
            mts = await client.get_mini_tickers()
        assert len(mts) > 0

    async def test_get_book_tickers_returns_typed_list(self):
        client = _build_client_returning(V2["spot_markets_book_tickers"]["response"])
        async with client:
            bts = await client.get_book_tickers()
        assert len(bts) > 0


# ---------------------------------------------------------------------------
# Account reads
# ---------------------------------------------------------------------------


class TestAccountReads:
    async def test_get_balances(self):
        client = _build_client_returning(V2["spot_account_balances"]["response"])
        async with client:
            bal = await client.get_balances(_BURNER)
        assert bal.balances == []

    async def test_get_state_from_v3_capture(self):
        """V.3's spot_account_state has the burner with 1000 vUSDC —
        exercises both the wrapper and the BalanceEntry shape."""
        client = _build_client_returning(V3["spot_account_state"]["response"])
        async with client:
            state = await client.get_state(_BURNER)
        assert state.aid == 57436
        assert state.balances is not None and len(state.balances) == 1
        assert state.balances[0].asset == "vUSDC"
        assert state.balances[0].total == "1000"

    async def test_get_open_orders_empty(self):
        client = _build_client_returning(V2["spot_account_orders_open"]["response"])
        async with client:
            orders = await client.get_open_orders(_BURNER)
        assert orders.orders == []

    async def test_get_fee_rate(self):
        client = _build_client_returning(V2["spot_account_fee_rate"]["response"])
        async with client:
            fr = await client.get_fee_rate(_BURNER)
        assert isinstance(fr.maker_fee_rate, str)
        assert isinstance(fr.taker_fee_rate, str)

    async def test_get_api_keys_empty(self):
        """V.2 capture: pre-registration, no keys."""
        client = _build_client_returning(V2["spot_account_api_keys"]["response"])
        async with client:
            keys = await client.get_api_keys(_BURNER)
        assert keys == []


# ---------------------------------------------------------------------------
# Signed writes
# ---------------------------------------------------------------------------


class TestSignedWrites:
    async def test_submit_batch_new_passes_body_and_auth_headers(self):
        """V.3-shape success response (synthesised from docs — V.3
        actual perps_batch_new returned per-order rejection; same
        envelope structure)."""
        client, captured = _build_client_capturing_request(
            {
                "code": 0,
                "timestamp": 1779222797493,
                "data": [{"code": 0, "clOrdID": "etfpulse-spot-1", "orderID": 999}],
            }
        )
        body = b'{"accountID":57436,"orders":[]}'
        async with client:
            items = await client.submit_batch_new_order(
                body_bytes=body,
                typed_signature="0x01abcdef",
                nonce=1700000000000,
                api_key_name="default",
            )

        # Wire-level pin-down:
        # - exact body bytes (no re-serialisation)
        # - case-sensitive X-API-Key header
        # - api_key_name flows verbatim (the V.3-fixed bug)
        assert captured["method"] == "POST"
        assert captured["content"] == body
        assert captured["headers"]["x-api-key"] == "default"
        assert captured["headers"]["x-api-sign"] == "0x01abcdef"
        assert captured["headers"]["x-api-nonce"] == "1700000000000"

        # Parsed response
        assert len(items) == 1
        assert items[0].code == 0
        assert items[0].order_id == 999
        assert items[0].cl_ord_id == "etfpulse-spot-1"

    async def test_submit_batch_new_per_order_rejection(self):
        """V.3-shape: outer code 0, inner per-order code -1. The
        client returns the items as-is — caller decides how to handle
        partial-success batches."""
        client, _ = _build_client_capturing_request(
            {
                "code": 0,
                "timestamp": 1779222797493,
                "data": [
                    {
                        "code": -1,
                        "clOrdID": "etfpulse-spot-1",
                        "error": "lot size filter check failed",
                    }
                ],
            }
        )
        async with client:
            items = await client.submit_batch_new_order(
                body_bytes=b"{}",
                typed_signature="0x01ab",
                nonce=1,
                api_key_name="default",
            )
        assert len(items) == 1
        assert items[0].code == -1
        assert items[0].error == "lot size filter check failed"
        assert items[0].order_id is None

    async def test_submit_batch_new_auth_failure_raises_envelope_error(self):
        """Outer envelope `code: -1, error: 'API key not found'`
        — verified V.3 capture. Must raise `SodexAuthError`."""
        client, _ = _build_client_capturing_request(
            {
                "code": -1,
                "timestamp": 1779222796209,
                "error": "API key error: API key not found",
            }
        )
        with pytest.raises(SodexAuthError):
            async with client:
                await client.submit_batch_new_order(
                    body_bytes=b"{}",
                    typed_signature="0x01ab",
                    nonce=1,
                    api_key_name="wrong-name",
                )

    async def test_submit_batch_cancel_uses_delete(self):
        client, captured = _build_client_capturing_request(
            {
                "code": 0,
                "timestamp": 1779222797892,
                "data": [{"code": -1, "error": "order rejected: OrderNotFound"}],
            }
        )
        body = b'{"accountID":57436,"cancels":[]}'
        async with client:
            items = await client.submit_batch_cancel_order(
                body_bytes=body,
                typed_signature="0x01ab",
                nonce=1700000000001,
                api_key_name="default",
            )
        assert captured["method"] == "DELETE"
        assert captured["content"] == body
        assert items[0].code == -1
        assert items[0].cl_ord_id is None  # V.3 quirk — clOrdID absent on cancel-reject

    async def test_submit_write_with_null_data_raises_parse_error(self):
        """A `code: 0` envelope with `data == null` would be a wire
        contract violation — `OrderResponseItem` list is mandatory on
        success. Surface via `SodexParseError` so the caller can't
        accidentally treat it as a silent empty-batch success."""
        client, _ = _build_client_capturing_request({"code": 0, "timestamp": 1, "data": None})
        with pytest.raises(SodexParseError):
            async with client:
                await client.submit_batch_new_order(
                    body_bytes=b"{}",
                    typed_signature="0x01ab",
                    nonce=1,
                    api_key_name="default",
                )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestLowerAddress:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (
                "0xCABa55de67F421BDDb0813961A20988885aa98d7",
                "0xcaba55de67f421bddb0813961a20988885aa98d7",
            ),
            (
                "0xcaba55de67f421bddb0813961a20988885aa98d7",
                "0xcaba55de67f421bddb0813961a20988885aa98d7",
            ),
            # Missing 0x prefix gets added.
            (
                "caba55de67f421bddb0813961a20988885aa98d7",
                "0xcaba55de67f421bddb0813961a20988885aa98d7",
            ),
        ],
    )
    def test_normalisation(self, raw: str, expected: str):
        assert lower_address(raw) == expected


class TestSignedWriteHeaders:
    def test_headers_use_case_sensitive_keys(self):
        h = signed_write_headers(
            api_key_name="default",
            typed_signature="0x01abcdef",
            nonce=1700000000000,
        )
        assert h == {
            "X-API-Key": "default",
            "X-API-Sign": "0x01abcdef",
            "X-API-Nonce": "1700000000000",
        }
        # NO http-lowercased keys. The gateway treats these as case-
        # sensitive per V.3 verification.
