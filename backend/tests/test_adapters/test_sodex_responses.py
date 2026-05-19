"""Tests for `etfpulse.adapters.sodex.responses` — the Pydantic DTOs that
mirror SoDEX response shapes.

The contract here is byte-exact-against-the-fixture: every captured
response (V.2 reads + V.3 writes) must round-trip through the DTO
without losing fields and without raising. Drift in field names or
types breaks D.2's downstream parsing, so we replay each probe
explicitly rather than relying on generic schema checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etfpulse.adapters.sodex.responses import (
    AccountBalances,
    APIKey,
    BalanceEntry,
    BookTicker,
    Coin,
    FeeRate,
    MiniTicker,
    OpenOrdersResponse,
    OpenPositionsResponse,
    OrderResponseItem,
    PerpsAccountState,
    PerpsMarkPrice,
    PerpsSymbol,
    SpotAccountState,
    SpotSymbol,
    Ticker,
)

_V2_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sodex_endpoint_responses.json"
)
_V3_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sodex_signed_write_responses.json"
)


def _load_v2_probes() -> dict[str, object]:
    """Index V.2 captures by `name` for easy lookup in parametrized tests."""
    with open(_V2_PATH, encoding="utf-8") as fh:
        d = json.load(fh)
    return {p["name"]: p for p in d["probes"]}


def _load_v3_probes() -> dict[str, object]:
    with open(_V3_PATH, encoding="utf-8") as fh:
        d = json.load(fh)
    return {p["name"]: p for p in d["probes"]}


V2 = _load_v2_probes()
V3 = _load_v3_probes()


# ---------------------------------------------------------------------------
# Market endpoints — V.2 captures (read-only, no signing required)
# ---------------------------------------------------------------------------


class TestMarketDTOs:
    """Each market endpoint returns an array of objects. We parse every
    row in each captured list (not just the first) to catch shape drift
    on rare-shape items (e.g. delisted symbols with weird status)."""

    def test_spot_symbols_round_trip(self):
        data = V2["spot_markets_symbols"]["response"]["data"]
        assert len(data) > 0
        parsed = [SpotSymbol.model_validate(row) for row in data]
        # Spot-check the canonical vETH symbol — used by V.3 captures
        # so its parsing has to work for D.2's smoke tests.
        veth = next(s for s in parsed if s.name == "vETH_vUSDC")
        assert veth.id == 2
        assert veth.tick_size == "0.1"
        assert veth.min_notional == "5"

    def test_perps_symbols_round_trip(self):
        data = V2["perps_markets_symbols"]["response"]["data"]
        assert len(data) > 0
        parsed = [PerpsSymbol.model_validate(row) for row in data]
        eth_usd = next(s for s in parsed if s.name == "ETH-USD")
        assert eth_usd.id == 2
        assert eth_usd.init_leverage > 0
        assert eth_usd.max_leverage >= eth_usd.init_leverage
        # margin_tiers is preserved as raw list (D.3 will type it later).
        assert isinstance(eth_usd.margin_tiers, list)

    @pytest.mark.parametrize("probe_name", ["spot_markets_coins", "perps_markets_coins"])
    def test_coins_round_trip(self, probe_name: str):
        data = V2[probe_name]["response"]["data"]
        parsed = [Coin.model_validate(row) for row in data]
        assert len(parsed) > 0
        # Spot rows have no margin_ratio / price; perps rows do.
        if probe_name.startswith("perps"):
            assert any(c.margin_ratio is not None for c in parsed)
        else:
            assert all(c.margin_ratio is None for c in parsed)

    @pytest.mark.parametrize("probe_name", ["spot_markets_tickers", "perps_markets_tickers"])
    def test_tickers_round_trip(self, probe_name: str):
        data = V2[probe_name]["response"]["data"]
        parsed = [Ticker.model_validate(row) for row in data]
        assert len(parsed) > 0
        # Perps tickers include funding info; spot ones don't.
        if probe_name.startswith("perps"):
            assert any(t.funding_rate is not None for t in parsed)
            assert any(t.mark_price is not None for t in parsed)
        else:
            assert all(t.funding_rate is None for t in parsed)

    def test_mini_tickers_round_trip(self):
        data = V2["spot_markets_mini_tickers"]["response"]["data"]
        parsed = [MiniTicker.model_validate(row) for row in data]
        assert len(parsed) > 0
        assert all(t.symbol for t in parsed)

    def test_book_tickers_round_trip(self):
        data = V2["spot_markets_book_tickers"]["response"]["data"]
        parsed = [BookTicker.model_validate(row) for row in data]
        assert len(parsed) > 0
        # bid_px / ask_px are quoted-string decimals.
        assert all(isinstance(t.bid_px, str) for t in parsed)

    def test_perps_mark_prices_round_trip(self):
        data = V2["perps_markets_mark_prices"]["response"]["data"]
        parsed = [PerpsMarkPrice.model_validate(row) for row in data]
        assert len(parsed) > 0
        # mark_price + index_price are present for every entry.
        assert all(p.mark_price and p.index_price for p in parsed)


# ---------------------------------------------------------------------------
# Account endpoints — read shape from V.2, balance entries from V.3
# (V.3 has a non-empty burner so the balance entry shape is exercised).
# ---------------------------------------------------------------------------


class TestAccountDTOs:
    def test_spot_balances_round_trip(self):
        """V.2 capture: empty (burner had no balance at probe time).
        Tests the wrapper shape — `balances` defaults to empty list.

        `block_height` and `block_time` are both `0` in the V.2 capture
        because the burner had no balance state yet; we assert `>= 0`
        rather than `> 0` to mirror the actual wire shape."""
        data = V2["spot_account_balances"]["response"]["data"]
        parsed = AccountBalances.model_validate(data)
        assert parsed.balances == []
        assert parsed.block_height >= 0
        assert parsed.block_time >= 0

    def test_balance_entry_short_keys(self):
        """V.3 captured the burner with 1000 vUSDC — exercises the
        compact `{i, a, t, l}` shape that `BalanceEntry` mirrors."""
        data = V3["spot_account_state"]["response"]["data"]
        state = SpotAccountState.model_validate(data)
        assert state.balances is not None
        assert len(state.balances) == 1
        entry = state.balances[0]
        assert entry.instrument_id == 0
        assert entry.asset == "vUSDC"
        assert entry.total == "1000"
        assert entry.locked == "0"

    def test_spot_state_null_orders(self):
        """V.3 capture: `O: null` when no open orders. The DTO must
        accept `None` and not default-to-empty-list silently."""
        data = V3["spot_account_state"]["response"]["data"]
        state = SpotAccountState.model_validate(data)
        assert state.orders is None  # explicit null preserved
        assert state.aid == 57436

    def test_perps_state_round_trip(self):
        """V.3 perps state: zero-balance burner, all margin fields are
        the string '0'. Tests that all 8 margin field aliases parse."""
        data = V3["perps_account_state"]["response"]["data"]
        state = PerpsAccountState.model_validate(data)
        assert state.aid == 57436
        # Every margin field is a string, defaults to '0' on empty.
        assert state.account_value == "0"
        assert state.account_margin == "0"
        assert state.initial_margin == "0"
        assert state.current_margin == "0"
        # All nullable arrays come back as None when empty.
        assert state.balances is None
        assert state.positions is None
        assert state.orders is None
        assert state.sub_state is None

    @pytest.mark.parametrize(
        "probe_name", ["spot_account_orders_open", "perps_account_orders_open"]
    )
    def test_open_orders_round_trip(self, probe_name: str):
        data = V2[probe_name]["response"]["data"]
        parsed = OpenOrdersResponse.model_validate(data)
        # Both venues captured empty `orders` list (burner had no
        # open orders at probe time).
        assert parsed.orders == []
        assert parsed.block_height >= 0

    def test_perps_positions_round_trip(self):
        """Perps positions uses wire-key `orders` (gateway quirk),
        aliased to `positions` in our DTO so the name reflects reality."""
        data = V2["perps_account_positions"]["response"]["data"]
        parsed = OpenPositionsResponse.model_validate(data)
        assert parsed.positions == []  # burner has no positions
        assert parsed.block_height >= 0

    @pytest.mark.parametrize("probe_name", ["spot_account_fee_rate", "perps_account_fee_rate"])
    def test_fee_rate_round_trip(self, probe_name: str):
        data = V2[probe_name]["response"]["data"]
        parsed = FeeRate.model_validate(data)
        # Quoted-string decimals — they come through as `str`.
        assert isinstance(parsed.maker_fee_rate, str)
        assert isinstance(parsed.taker_fee_rate, str)

    @pytest.mark.parametrize("probe_name", ["spot_account_api_keys", "perps_account_api_keys"])
    def test_api_keys_empty_in_v2(self, probe_name: str):
        """V.2 capture: empty list (burner not yet registered).
        Validates the parser accepts the empty case."""
        data = V2[probe_name]["response"]["data"]
        assert data == []
        # No DTO call needed — empty list has no entries to validate.

    def test_api_key_entry_shape_from_v3_aware_endpoint(self):
        """We can't replay this from V.3 (signed-write fixture doesn't
        include /api-keys) but we can synthesize from the documented
        shape we observed via curl after registration:
            {"name": "default", "type": "EVM",
             "publicKey": "0xcaba55...", "expiresAt": 0}
        """
        row = {
            "name": "default",
            "type": "EVM",
            "publicKey": "0xcaba55de67f421bddb0813961a20988885aa98d7",
            "expiresAt": 0,
        }
        parsed = APIKey.model_validate(row)
        assert parsed.name == "default"
        assert parsed.type == "EVM"
        assert parsed.public_key == "0xcaba55de67f421bddb0813961a20988885aa98d7"
        assert parsed.expires_at == 0


# ---------------------------------------------------------------------------
# Write-path response item — the dual-layer envelope's inner array
# ---------------------------------------------------------------------------


class TestOrderResponseItem:
    """The inner `data[i]` shape of POST /trade/orders[/batch] +
    DELETE /trade/orders[/batch]. Each item carries its own `code`,
    independent of the outer envelope. Captured both rejection cases
    via V.3 (auth-rejected spot side; application-rejected perps
    side after auth passed)."""

    def test_per_order_rejection_shape_perps_new(self):
        """V.3 perps_batch_new — outer envelope `code: 0`, but the
        single inner item is `code: -1` ("insufficient margin")
        because the burner's perps balance was zero."""
        data = V3["perps_batch_new"]["response"]["data"]
        assert isinstance(data, list) and len(data) == 1
        item = OrderResponseItem.model_validate(data[0])
        assert item.code == -1
        assert item.error == "insufficient margin"
        assert item.cl_ord_id == "v3-perps-1779222797167"
        assert item.order_id is None  # rejected → no orderID

    def test_per_cancel_rejection_shape_perps_cancel(self):
        """V.3 perps_batch_cancel — same outer `code: 0`, inner item
        is `code: -1` ("order rejected: OrderNotFound"). NOTE: the
        capture has NO `clOrdID` field on the inner item even though
        the cancel request specified one. SoDEX-side quirk — our DTO
        marks clOrdID Optional to match."""
        data = V3["perps_batch_cancel"]["response"]["data"]
        assert isinstance(data, list) and len(data) == 1
        item = OrderResponseItem.model_validate(data[0])
        assert item.code == -1
        assert item.error == "order rejected: OrderNotFound"
        assert item.cl_ord_id is None
        assert item.order_id is None

    def test_per_order_success_synthesized_shape(self):
        """The success-case inner shape is documented at
        rest-v1/sodex-rest-spot-api.md §"Place multiple orders" but
        wasn't captured in V.3 (perps had zero balance, spot was
        venue-misregistered). Synthesise from docs so D.2's parser
        is exercised for the success path too — D.5 live smoke
        catches any divergence."""
        row = {
            "code": 0,
            "clOrdID": "test-1",
            "orderID": 123456789,
        }
        item = OrderResponseItem.model_validate(row)
        assert item.code == 0
        assert item.cl_ord_id == "test-1"
        assert item.order_id == 123456789
        assert item.error is None


# ---------------------------------------------------------------------------
# Forward-compatibility: extra fields are ignored
# ---------------------------------------------------------------------------


class TestExtraFieldsIgnored:
    """`extra="ignore"` is the documented forward-compatibility
    posture. If SoDEX adds a new field to any response, our parser
    accepts it without raising — the new field is simply dropped on
    the floor until we want to surface it."""

    def test_unknown_field_in_balance_entry_ignored(self):
        row = {
            "i": 0,
            "a": "vUSDC",
            "t": "1000",
            "l": "0",
            "newSecretField": "x",  # not in DTO
        }
        # Must NOT raise.
        entry = BalanceEntry.model_validate(row)
        assert entry.total == "1000"

    def test_populate_by_name_for_python_construction(self):
        """Tests + future internal callers can construct via Pythonic
        names (snake_case) — the alias is only required for wire JSON."""
        entry = BalanceEntry(instrument_id=1, asset="vETH", total="0.5", locked="0.1")
        assert entry.instrument_id == 1
        assert entry.asset == "vETH"
