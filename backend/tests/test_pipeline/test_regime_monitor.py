"""regime_monitor — pure scoring helpers + DB-backed integration tests.

Pure helpers (`_regime_from_score`, `_confidence_from_score`,
`_filter_events_nearby`) are tested directly. The async `classify_regime`
function is exercised against the test DB plus the fixture-mode SoSoValue
adapter so we cover the macro-event override path end-to-end.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from etfpulse.adapters.sosovalue import MacroEvent, sosovalue_client
from etfpulse.models import (
    ETFFlow,
    MarketRegime,
    NewsItem,
    SignalPosture,
)
from etfpulse.pipeline.regime_monitor import (
    _confidence_from_score,
    _filter_events_nearby,
    _regime_from_score,
    classify_regime,
    get_latest_regime,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestRegimeFromScore:
    """Boundary tests — every threshold edge has a pinned outcome."""

    def test_strong_positive_is_markup(self):
        assert _regime_from_score(60) == MarketRegime.MARKUP
        assert _regime_from_score(40) == MarketRegime.MARKUP  # threshold

    def test_weak_positive_is_accumulation(self):
        assert _regime_from_score(20) == MarketRegime.ACCUMULATION
        assert _regime_from_score(10) == MarketRegime.ACCUMULATION  # threshold

    def test_zero_is_uncertain(self):
        assert _regime_from_score(0) == MarketRegime.UNCERTAIN
        assert _regime_from_score(5) == MarketRegime.UNCERTAIN
        assert _regime_from_score(-5) == MarketRegime.UNCERTAIN

    def test_weak_negative_is_distribution(self):
        assert _regime_from_score(-20) == MarketRegime.DISTRIBUTION
        assert _regime_from_score(-10) == MarketRegime.DISTRIBUTION  # threshold

    def test_strong_negative_is_markdown(self):
        assert _regime_from_score(-60) == MarketRegime.MARKDOWN
        assert _regime_from_score(-40) == MarketRegime.MARKDOWN  # threshold


class TestConfidenceFromScore:
    def test_zero_score_is_min_confidence(self):
        assert _confidence_from_score(0) == 1

    def test_max_score_is_capped_at_ten(self):
        assert _confidence_from_score(50) == 10
        assert _confidence_from_score(100) == 10  # over-cap is clamped

    def test_negative_uses_magnitude(self):
        assert _confidence_from_score(-25) == _confidence_from_score(25)

    def test_in_range(self):
        for score in range(-100, 101, 5):
            c = _confidence_from_score(score)
            assert 1 <= c <= 10


class TestFilterEventsNearby:
    def test_inside_window_included(self):
        today = date(2026, 4, 22)
        events = [
            MacroEvent(date=today, events=["Today Event"]),
            MacroEvent(date=today + timedelta(days=2), events=["Plus 2 Event"]),
            MacroEvent(date=today - timedelta(days=2), events=["Minus 2 Event"]),
        ]
        labels = _filter_events_nearby(events, today)
        assert "Today Event" in labels
        assert "Plus 2 Event" in labels
        assert "Minus 2 Event" in labels

    def test_outside_window_excluded(self):
        today = date(2026, 4, 22)
        events = [
            MacroEvent(date=today + timedelta(days=3), events=["Far Future"]),
            MacroEvent(date=today - timedelta(days=10), events=["Long Past"]),
        ]
        assert _filter_events_nearby(events, today) == []

    def test_multiple_events_per_day_all_included(self):
        today = date(2026, 4, 22)
        events = [MacroEvent(date=today, events=["FOMC", "CPI"])]
        assert _filter_events_nearby(events, today) == ["FOMC", "CPI"]


# ---------------------------------------------------------------------------
# DB integration
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_macro_events(monkeypatch):
    """Make the macro adapter return empty so we test the no-override path."""

    async def _empty(page=1, page_size=10):
        return []

    monkeypatch.setattr(sosovalue_client, "get_macro_events", _empty)


@pytest.fixture
def _sector_spotlight_fails(monkeypatch):
    """Force the sector-spotlight call to raise so we can test the
    `available: False` reasoning + NULL btc_dominance path."""
    from etfpulse.adapters.sosovalue import SoSoValueError

    async def _raise():
        raise SoSoValueError("simulated network error")

    monkeypatch.setattr(sosovalue_client, "get_sector_spotlight", _raise)


class TestClassifyRegime:
    async def test_empty_db_yields_uncertain(self, db_session, _no_macro_events):
        """No flows, no news, no macro → score 0 → UNCERTAIN, posture from default
        map (CAUTIOUS for UNCERTAIN). Confidence = 1 (low magnitude).
        Sector-spotlight fixture provides BTC dominance — value is persisted on
        the snapshot but does not affect the score (see classify_regime
        docstring for rationale)."""
        c = await classify_regime(db_session)
        assert c.regime == MarketRegime.UNCERTAIN
        assert c.signal_posture == SignalPosture.CAUTIOUS
        assert c.confidence == 1
        assert c.macro_events_nearby == []
        # Dominance is sourced from sosovalue_sector_spotlight.json fixture
        # which records BTC at 0.5944 marketcap_dom.
        assert c.reasoning["dominance"]["available"] is True
        assert c.btc_dominance == Decimal("0.5944")
        assert c.reasoning["dominance"]["btc_dominance"] == "0.5944"

    async def test_dominance_unavailable_when_adapter_fails(
        self, db_session, _no_macro_events, _sector_spotlight_fails
    ):
        """SoSoValueError on sector-spotlight is non-fatal — classifier still
        returns a regime; btc_dominance is None and reasoning records the
        fetch_error rather than a value."""
        c = await classify_regime(db_session)
        assert c.regime == MarketRegime.UNCERTAIN
        assert c.btc_dominance is None
        assert c.reasoning["dominance"]["available"] is False
        assert c.reasoning["dominance"]["fetch_error"] == "SoSoValueError"

    async def test_strong_positive_flows_drive_markup(self, db_session, _no_macro_events):
        """Seed 7d of large positive BTC flows → score saturates positive →
        MARKUP regime, NORMAL default posture."""
        today = datetime.now(UTC).date()
        for i in range(7):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=today - timedelta(days=i),
                    total_net_flow_usd=Decimal("1_500_000_000"),
                )
            )
        await db_session.flush()

        c = await classify_regime(db_session)
        assert c.regime == MarketRegime.MARKUP
        assert c.signal_posture == SignalPosture.NORMAL
        assert c.confidence >= 8

    async def test_macro_events_override_posture_to_cautious(self, db_session, monkeypatch):
        """Even with strong directional flows, macro_events_nearby forces
        posture=cautious. Regime itself is unchanged (bug-fix from stage doc)."""
        today = datetime.now(UTC).date()
        for i in range(7):
            db_session.add(
                ETFFlow(
                    asset="BTC",
                    captured_at=today - timedelta(days=i),
                    total_net_flow_usd=Decimal("1_500_000_000"),
                )
            )
        await db_session.flush()

        async def _events(page=1, page_size=10):
            return [MacroEvent(date=today, events=["FOMC"])]

        monkeypatch.setattr(sosovalue_client, "get_macro_events", _events)

        c = await classify_regime(db_session)
        # Regime survives the override — the bug we're guarding against had
        # the regime undefined in the macro-event branch.
        assert c.regime == MarketRegime.MARKUP
        assert c.signal_posture == SignalPosture.CAUTIOUS
        assert "FOMC" in c.macro_events_nearby

    async def test_news_velocity_pulls_score_negative(self, db_session, _no_macro_events):
        """A burst of news without strong flows pulls the score down enough
        to keep us in UNCERTAIN — velocity is uncertainty, not direction."""
        today = datetime.now(UTC).date()
        # Mild positive flows that would otherwise be ACCUMULATION.
        db_session.add(
            ETFFlow(
                asset="BTC",
                captured_at=today,
                total_net_flow_usd=Decimal("500_000_000"),
            )
        )
        # 100 news items in last 24h → max -15 news pull.
        for i in range(100):
            db_session.add(
                NewsItem(
                    source_id=f"news-velocity-{i}",
                    category=3,
                    captured_at=datetime.now(UTC),
                )
            )
        await db_session.flush()

        c = await classify_regime(db_session)
        assert c.reasoning["news"]["velocity_count"] == 100
        assert c.reasoning["news"]["score"] == -15

    async def test_macro_fetch_failure_is_soft(self, db_session, monkeypatch):
        """SoSoValue down → classifier returns a regime + records the gap in
        reasoning. No exception propagates."""
        from etfpulse.adapters.sosovalue import SoSoValueError

        async def _broken(page=1, page_size=10):
            raise SoSoValueError("simulated outage")

        monkeypatch.setattr(sosovalue_client, "get_macro_events", _broken)

        c = await classify_regime(db_session)
        assert c.regime is not None
        assert c.reasoning["macro"]["fetch_error"] == "SoSoValueError"
        assert c.macro_events_nearby == []


class TestGetLatestRegime:
    async def test_returns_none_on_empty_table(self, db_session):
        assert await get_latest_regime(db_session) is None

    async def test_returns_most_recent(self, db_session):
        from etfpulse.models import RegimeSnapshot

        old = RegimeSnapshot(
            captured_at=datetime.now(UTC) - timedelta(days=1),
            regime=MarketRegime.ACCUMULATION.value,
        )
        new = RegimeSnapshot(
            captured_at=datetime.now(UTC),
            regime=MarketRegime.MARKUP.value,
        )
        db_session.add(old)
        db_session.add(new)
        await db_session.flush()

        latest = await get_latest_regime(db_session)
        assert latest is not None
        assert latest.regime == MarketRegime.MARKUP.value
