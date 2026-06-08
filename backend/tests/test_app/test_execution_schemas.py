"""PR P1.2 — `PrepareNewRequest` stop / reduce_only / parent_order_id field validation.

Pins the API-boundary invariants the DB CHECKs encode at the row level
(`ck_orders_stop_price_positive`, `ck_orders_stop_type_enum`,
`ck_orders_stop_price_type_consistency`) so a malformed body becomes a
clean 422, not a 500 from IntegrityError downstream.

Validators tested here are positive (accepts every valid combo) and
negative (rejects every malformed combo). Risk-engine rules (perps-only
gating, reduce-only requires parent, etc.) live in P1.3 and are pinned
by `tests/test_pipeline/test_execution_risk.py`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from etfpulse.api.schemas.execution import (
    ApiOrderType,
    ApiSide,
    ApiStopType,
    ApiTimeInForce,
    PrepareNewRequest,
)
from etfpulse.models.order import StopType


def _base_body() -> dict:
    """Minimum-valid PrepareNewRequest body — tests override fields."""
    return {
        "venue": "sodex_perps",
        "asset": "BTC",
        "side": ApiSide.BUY.value,
        "order_type": ApiOrderType.LIMIT.value,
        "time_in_force": ApiTimeInForce.GTC.value,
        "requested_size": "0.01",
        "requested_price": "65000",
    }


class TestApiStopTypeMirror:
    """ApiStopType wire values MUST equal StopType DB literals exactly.
    A drift would let a 200 schema-pass become a 500 IntegrityError."""

    def test_mirror_complete(self):
        assert {s.value for s in ApiStopType} == {s.value for s in StopType}


class TestStopFieldsOmittedDefault:
    """Backwards compat: existing callers without stop fields still work."""

    def test_defaults(self):
        m = PrepareNewRequest.model_validate(_base_body())
        assert m.stop_price is None
        assert m.stop_type is None
        assert m.reduce_only is False
        assert m.parent_order_id is None


class TestStopFieldsAcceptedWhenBothSet:
    @pytest.mark.parametrize("stop_type", [s.value for s in ApiStopType])
    def test_each_stop_type_accepted(self, stop_type):
        body = {**_base_body(), "stop_price": "60000", "stop_type": stop_type}
        m = PrepareNewRequest.model_validate(body)
        assert m.stop_price == Decimal("60000")
        assert m.stop_type == ApiStopType(stop_type)


class TestStopPricePositive:
    @pytest.mark.parametrize("bad", ["0", "-1", "-0.00000001"])
    def test_non_positive_rejected(self, bad):
        body = {
            **_base_body(),
            "stop_price": bad,
            "stop_type": ApiStopType.STOP_LOSS.value,
        }
        with pytest.raises(ValidationError) as exc:
            PrepareNewRequest.model_validate(body)
        # Field-level constraint produces a field-path error; we only
        # assert the offending field appears, not the exact wording.
        assert "stop_price" in str(exc.value)


class TestStopTypeEnumGuard:
    def test_unknown_literal_rejected(self):
        body = {**_base_body(), "stop_price": "60000", "stop_type": "trailing_stop"}
        with pytest.raises(ValidationError) as exc:
            PrepareNewRequest.model_validate(body)
        assert "stop_type" in str(exc.value)


class TestStopCoOccurrence:
    """Mirrors `ck_orders_stop_price_type_consistency`."""

    def test_price_without_type_rejected(self):
        body = {**_base_body(), "stop_price": "60000"}
        with pytest.raises(ValidationError) as exc:
            PrepareNewRequest.model_validate(body)
        assert "stop_price and stop_type must be set together" in str(exc.value)

    def test_type_without_price_rejected(self):
        body = {**_base_body(), "stop_type": ApiStopType.STOP_LOSS.value}
        with pytest.raises(ValidationError) as exc:
            PrepareNewRequest.model_validate(body)
        assert "stop_price and stop_type must be set together" in str(exc.value)

    def test_both_null_accepted(self):
        # Already covered by TestStopFieldsOmittedDefault, but the
        # explicit-None path goes through a different validator branch.
        body = {**_base_body(), "stop_price": None, "stop_type": None}
        m = PrepareNewRequest.model_validate(body)
        assert m.stop_price is None
        assert m.stop_type is None


class TestReduceOnly:
    def test_default_false(self):
        m = PrepareNewRequest.model_validate(_base_body())
        assert m.reduce_only is False

    @pytest.mark.parametrize("v", [True, False])
    def test_explicit(self, v):
        m = PrepareNewRequest.model_validate({**_base_body(), "reduce_only": v})
        assert m.reduce_only is v


class TestParentOrderId:
    @pytest.mark.parametrize("val", [1, 42, 9_000_000_000])
    def test_positive_accepted(self, val):
        m = PrepareNewRequest.model_validate({**_base_body(), "parent_order_id": val})
        assert m.parent_order_id == val

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_rejected(self, bad):
        with pytest.raises(ValidationError) as exc:
            PrepareNewRequest.model_validate({**_base_body(), "parent_order_id": bad})
        assert "parent_order_id" in str(exc.value)
