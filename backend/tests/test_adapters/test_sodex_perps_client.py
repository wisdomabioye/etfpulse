"""Replay tests for `SodexPerpsClient` against V.2 + V.3 fixtures.
Mirror of `test_sodex_spot_client.py` for the perps venue —
exercises the perps-specific endpoints (`/mark-prices`, `/positions`)
and the perps trade paths (`/trade/orders` without `/batch`).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from etfpulse.adapters.sodex._http import (
    SodexHttpClient,
    SodexValidationError,
)
from etfpulse.adapters.sodex.perps_client import SodexPerpsClient

_BURNER = "0xcaba55de67f421bddb0813961a20988885aa98d7"

_V2 = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sodex_endpoint_responses.json"
_V3 = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sodex_signed_write_responses.json"
)


def _load(path: Path) -> dict[str, object]:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return {p["name"]: p for p in d["probes"]}


V2 = _load(_V2)
V3 = _load(_V3)


def _build(body: object) -> SodexPerpsClient:
    payload = json.dumps(body).encode("utf-8")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, content=payload)

    transport = httpx.MockTransport(handler)
    http = SodexHttpClient(
        base_url="https://example.test/api/v1/perps",
        timeout_seconds=5.0,
        retry_max_attempts=1,
        retry_base_seconds=0.001,
        transport=transport,
    )
    return SodexPerpsClient(http)


def _build_capturing(body: object) -> tuple[SodexPerpsClient, dict]:
    captured: dict = {}
    payload = json.dumps(body).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["content"] = bytes(request.content)
        return httpx.Response(status_code=200, content=payload)

    transport = httpx.MockTransport(handler)
    http = SodexHttpClient(
        base_url="https://example.test/api/v1/perps",
        timeout_seconds=5.0,
        retry_max_attempts=1,
        retry_base_seconds=0.001,
        transport=transport,
    )
    return SodexPerpsClient(http), captured


# ---------------------------------------------------------------------------
# Perps market reads
# ---------------------------------------------------------------------------


class TestPerpsMarketReads:
    async def test_get_symbols(self):
        client = _build(V2["perps_markets_symbols"]["response"])
        async with client:
            symbols = await client.get_symbols()
        eth_usd = next(s for s in symbols if s.name == "ETH-USD")
        assert eth_usd.id == 2
        # Perps-specific fields present.
        assert eth_usd.init_leverage > 0
        assert eth_usd.max_leverage > 0

    async def test_get_coins(self):
        client = _build(V2["perps_markets_coins"]["response"])
        async with client:
            coins = await client.get_coins()
        # Perps coins include margin_ratio + price (unlike spot).
        assert any(c.margin_ratio is not None for c in coins)

    async def test_get_tickers(self):
        client = _build(V2["perps_markets_tickers"]["response"])
        async with client:
            tickers = await client.get_tickers()
        # Perps tickers include funding info.
        assert any(t.funding_rate is not None for t in tickers)
        assert any(t.mark_price is not None for t in tickers)

    async def test_get_mark_prices(self):
        """Perps-only endpoint — verifies the venue client exposes it
        (SodexSpotClient does not)."""
        client = _build(V2["perps_markets_mark_prices"]["response"])
        async with client:
            mps = await client.get_mark_prices()
        assert len(mps) > 0
        assert all(p.mark_price and p.index_price for p in mps)


# ---------------------------------------------------------------------------
# Perps account reads
# ---------------------------------------------------------------------------


class TestPerpsAccountReads:
    async def test_get_state_includes_margin_fields(self):
        """V.3 perps_account_state — zero-balance burner. Validates
        the eight margin-field aliases parse + the all-null
        balances/positions/orders shape."""
        client = _build(V3["perps_account_state"]["response"])
        async with client:
            state = await client.get_state(_BURNER)
        assert state.aid == 57436
        assert state.account_value == "0"
        assert state.initial_margin == "0"
        assert state.balances is None
        assert state.positions is None
        assert state.orders is None

    async def test_get_balances(self):
        client = _build(V2["perps_account_balances"]["response"])
        async with client:
            bal = await client.get_balances(_BURNER)
        assert bal.balances == []

    async def test_get_positions_uses_aliased_field(self):
        """The wire returns `orders` on `/positions`; our DTO aliases
        it to `positions`. Client surface uses the corrected name."""
        client = _build(V2["perps_account_positions"]["response"])
        async with client:
            pos = await client.get_positions(_BURNER)
        assert pos.positions == []
        assert pos.block_height >= 0

    async def test_get_open_orders(self):
        client = _build(V2["perps_account_orders_open"]["response"])
        async with client:
            orders = await client.get_open_orders(_BURNER)
        assert orders.orders == []

    async def test_get_fee_rate(self):
        client = _build(V2["perps_account_fee_rate"]["response"])
        async with client:
            fr = await client.get_fee_rate(_BURNER)
        assert isinstance(fr.maker_fee_rate, str)

    async def test_get_api_keys_empty(self):
        client = _build(V2["perps_account_api_keys"]["response"])
        async with client:
            keys = await client.get_api_keys(_BURNER)
        assert keys == []


# ---------------------------------------------------------------------------
# Perps signed writes — exercises the actual V.3 success captures
# ---------------------------------------------------------------------------


class TestPerpsSignedWrites:
    async def test_submit_batch_new_path_is_trade_orders_no_batch(self):
        """Important wire detail: spot uses `/trade/orders/batch`,
        perps uses `/trade/orders` (no `/batch` suffix). Mistaking
        the two would hit a 404 from the gateway."""
        client, captured = _build_capturing(V3["perps_batch_new"]["response"])
        async with client:
            await client.submit_batch_new_order(
                body_bytes=b"{}",
                typed_signature="0x01ab",
                nonce=1,
                api_key_name="default",
            )
        # The URL ends with `/trade/orders`, not `/trade/orders/batch`.
        assert captured["url"].endswith("/trade/orders")
        assert captured["method"] == "POST"

    async def test_v3_per_order_insufficient_margin_replays_cleanly(self):
        """The actual V.3 capture: perps_batch_new succeeded at the
        envelope level (`code: 0`) but the inner per-order item was
        rejected with `insufficient margin` (burner has zero perps
        balance). End-to-end the client returns the typed item with
        code=-1, error preserved."""
        client = _build(V3["perps_batch_new"]["response"])
        async with client:
            items = await client.submit_batch_new_order(
                body_bytes=b"{}",
                typed_signature="0x01ab",
                nonce=1,
                api_key_name="default",
            )
        assert len(items) == 1
        assert items[0].code == -1
        assert items[0].error == "insufficient margin"
        assert items[0].cl_ord_id == "v3-perps-1779222797167"
        assert items[0].order_id is None

    async def test_v3_cancel_order_not_found_replays_cleanly(self):
        """Same dual-layer pattern for cancels — outer `code: 0`,
        inner rejection with `OrderNotFound`. Verifies the DELETE
        method gets used."""
        client, captured = _build_capturing(V3["perps_batch_cancel"]["response"])
        async with client:
            items = await client.submit_batch_cancel_order(
                body_bytes=b"{}",
                typed_signature="0x01ab",
                nonce=2,
                api_key_name="default",
            )
        assert captured["method"] == "DELETE"
        assert captured["url"].endswith("/trade/orders")
        assert items[0].code == -1
        assert items[0].error == "order rejected: OrderNotFound"

    async def test_envelope_error_does_not_return_items(self):
        """If the OUTER envelope is `code != 0`, we never reach the
        per-order layer — `SodexEnvelopeError` raises upstream."""
        client = _build(
            {
                "code": -1,
                "timestamp": 1,
                "error": "insufficient margin",  # not auth-class
            }
        )
        with pytest.raises(SodexValidationError):
            async with client:
                await client.submit_batch_new_order(
                    body_bytes=b"{}",
                    typed_signature="0x01ab",
                    nonce=1,
                    api_key_name="default",
                )
