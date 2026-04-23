"""DTO flattening + status-derivation tests.

The flattening logic is load-bearing — if a future refactor breaks how
`ai_analysis` is extracted, every signal response starts returning NULL
headlines with no immediate error. These tests pin the exact behavior.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from etfpulse.api.schemas.dashboard import DashboardStats
from etfpulse.api.schemas.signals import (
    PaginatedSignals,
    SignalDetail,
    SignalListItem,
    _derive_display_status,
    format_cursor,
    parse_cursor,
)
from etfpulse.models import Signal


def _make_signal(**overrides) -> Signal:
    """Factory for an ORM-shaped Signal with sensible defaults. Not persisted."""
    defaults = {
        "id": 42,
        "signal_type": "flow_anomaly",
        "asset": "BTC",
        "trigger_data": {"streak_length": 4},
        "ai_analysis": {
            "headline": "BTC 4-day streak ends",
            "reasoning": ["r1", "r2"],
            "confidence": 7,
            "risks": ["k1"],
            "suggested_action": "consider short",
            "time_horizon": "swing",
        },
        "confidence": 7,
        "status": "alerted",
        "fingerprint": "0" * 32,
        "signal_date": date(2026, 4, 21),
        "created_at": datetime(2026, 4, 21, 4, 32, tzinfo=UTC),
        "expires_at": datetime(2026, 4, 24, 4, 32, tzinfo=UTC),
    }
    defaults.update(overrides)
    # Build by attribute since Signal requires SQLAlchemy session to be useful
    # as an ORM row. We just need the attrs to match.
    signal = Signal()
    for k, v in defaults.items():
        setattr(signal, k, v)
    return signal


_NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)


# ---- SignalListItem: flattening --------------------------------------------


class TestSignalListItemFlattening:
    def test_flattens_ai_analysis_fields(self):
        signal = _make_signal()
        item = SignalListItem.from_row(signal, outcome_id=None, alerted_to=100, now=_NOW)

        assert item.headline == "BTC 4-day streak ends"
        assert item.suggested_action == "consider short"
        assert item.time_horizon == "swing"
        assert item.confidence == 7
        assert item.alerted_to == 100

    def test_null_ai_analysis_yields_null_flattened_fields(self):
        """AI-failed signals must round-trip through the DTO without error
        — NULL in, NULL out. The frontend handles rendering the fallback."""
        signal = _make_signal(ai_analysis=None, confidence=None)
        item = SignalListItem.from_row(signal, outcome_id=None, alerted_to=0, now=_NOW)

        assert item.headline is None
        assert item.suggested_action is None
        assert item.time_horizon is None
        assert item.confidence is None

    def test_partial_ai_analysis_uses_get_semantics(self):
        """If the AI returned a sparse response (only headline, missing
        time_horizon), we still get a coherent DTO — not a 500."""
        signal = _make_signal(ai_analysis={"headline": "only this"})
        item = SignalListItem.from_row(signal, outcome_id=None, alerted_to=0, now=_NOW)

        assert item.headline == "only this"
        assert item.suggested_action is None
        assert item.time_horizon is None


# ---- Status derivation -----------------------------------------------------


class TestDeriveDisplayStatus:
    def test_outcome_wins_over_everything(self):
        """`evaluated` is the most meaningful state — even if the signal is
        also past expires_at, the outcome measurement is what users care about."""
        signal = _make_signal(
            status="alerted",
            expires_at=datetime(2026, 4, 10, tzinfo=UTC),  # expired
        )
        assert _derive_display_status(signal, outcome_id=7, now=_NOW) == "evaluated"

    def test_expired_timestamp_beats_raw_alerted_status(self):
        """Signal was ALERTED in DB but the expiry ran past it. Display
        'expired' so users aren't misled about staleness."""
        signal = _make_signal(
            status="alerted",
            expires_at=datetime(2026, 4, 10, tzinfo=UTC),  # expired
        )
        assert _derive_display_status(signal, outcome_id=None, now=_NOW) == "expired"

    def test_future_expiry_preserves_raw_status(self):
        signal = _make_signal(
            status="alerted",
            expires_at=datetime(2026, 5, 1, tzinfo=UTC),  # future
        )
        assert _derive_display_status(signal, outcome_id=None, now=_NOW) == "alerted"

    def test_null_expires_at_preserves_raw_status(self):
        """NULL expires_at means 'never expires' (AI-failed signals) — raw
        DB status wins."""
        signal = _make_signal(status="pending", expires_at=None)
        assert _derive_display_status(signal, outcome_id=None, now=_NOW) == "pending"

    def test_raw_status_passes_through_unchanged(self):
        for raw in ("pending", "alerted", "expired"):
            signal = _make_signal(status=raw, expires_at=None)
            assert _derive_display_status(signal, outcome_id=None, now=_NOW) == raw


# ---- SignalDetail ----------------------------------------------------------


class TestSignalDetail:
    def test_renders_nested_ai_analysis(self):
        signal = _make_signal()
        detail = SignalDetail.from_row(signal, outcome=None, alerted_to=100, now=_NOW)

        assert detail.ai_analysis is not None
        assert detail.ai_analysis.headline == "BTC 4-day streak ends"
        assert detail.ai_analysis.reasoning == ["r1", "r2"]
        assert detail.ai_analysis.risks == ["k1"]
        assert detail.outcome is None

    def test_null_ai_analysis_field_is_none(self):
        signal = _make_signal(ai_analysis=None)
        detail = SignalDetail.from_row(signal, outcome=None, alerted_to=0, now=_NOW)
        assert detail.ai_analysis is None

    def test_full_32_char_fingerprint_exposed(self):
        """Backend returns full fingerprint — frontend truncates for display."""
        signal = _make_signal(fingerprint="a" * 32)
        detail = SignalDetail.from_row(signal, outcome=None, alerted_to=0, now=_NOW)
        assert detail.fingerprint == "a" * 32
        assert len(detail.fingerprint) == 32

    def test_trigger_data_preserved_raw(self):
        signal = _make_signal(trigger_data={"custom_field": 123, "nested": {"x": 1}})
        detail = SignalDetail.from_row(signal, outcome=None, alerted_to=0, now=_NOW)
        assert detail.trigger_data == {"custom_field": 123, "nested": {"x": 1}}


# ---- PaginatedSignals ------------------------------------------------------


class TestPaginatedSignals:
    def test_empty_page(self):
        page = PaginatedSignals(items=[], next_cursor=None)
        assert page.items == []
        assert page.next_cursor is None

    def test_with_cursor(self):
        page = PaginatedSignals(items=[], next_cursor="2026-04-20T04:32:00+00:00|38")
        assert page.next_cursor == "2026-04-20T04:32:00+00:00|38"


# ---- Cursor round-trip -----------------------------------------------------


class TestCursor:
    def test_format_and_parse_round_trip(self):
        ts = datetime(2026, 4, 22, 4, 32, 15, tzinfo=UTC)
        encoded = format_cursor(ts, 42)
        assert encoded == "2026-04-22T04:32:15+00:00|42"
        parsed = parse_cursor(encoded)
        assert parsed == (ts, 42)

    def test_malformed_cursor_returns_none(self):
        """Bad cursor → None, let the route 422 instead of 500."""
        assert parse_cursor("garbage") is None
        assert parse_cursor("2026-04-22|not-an-int") is None
        assert parse_cursor("not-a-date|42") is None
        assert parse_cursor("") is None

    def test_cursor_with_pipe_in_timestamp_handled_via_rsplit(self):
        """`rsplit(|, 1)` splits on the LAST pipe — an iso timestamp has no
        pipes so this is defensive, but verify the algorithm is correct."""
        # Fake case: if a timestamp ever contained a pipe somehow, rsplit
        # ensures the id is always the rightmost piece.
        assert parse_cursor("a|b|c|42") is None  # first 'a|b|c' isn't a valid datetime


# ---- DashboardStats --------------------------------------------------------


class TestDashboardStats:
    def test_valid_construction(self):
        stats = DashboardStats(
            total_signals=156,
            signals_today=2,
            avg_confidence=6.4,
            last_signal_at=datetime(2026, 4, 22, tzinfo=UTC),
        )
        assert stats.total_signals == 156
        assert stats.avg_confidence == 6.4

    def test_empty_db_state(self):
        """On a clean DB, the stats endpoint should return this shape."""
        stats = DashboardStats(
            total_signals=0,
            signals_today=0,
            avg_confidence=None,
            last_signal_at=None,
        )
        assert stats.avg_confidence is None
        assert stats.last_signal_at is None

    def test_negative_counts_rejected(self):
        with pytest.raises(ValidationError):
            DashboardStats(total_signals=-1, signals_today=0, avg_confidence=None)

    def test_out_of_range_confidence_rejected(self):
        with pytest.raises(ValidationError):
            DashboardStats(total_signals=1, signals_today=0, avg_confidence=11.0)
        with pytest.raises(ValidationError):
            DashboardStats(total_signals=1, signals_today=0, avg_confidence=0.5)
