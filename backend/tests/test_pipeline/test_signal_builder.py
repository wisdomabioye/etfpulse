"""signal_builder integration tests against the test DB.

We test orchestration, not detector logic — `ALL_DETECTORS` is monkey-patched
to a list of stubs so test outcomes don't depend on whether the SoSoValue
fixture data happens to trigger a real detector.

The five required cases (a–e from #44) are each a separate test below.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from etfpulse.models import MarketRegime, Signal, SignalPosture
from etfpulse.pipeline.analysis import AI_PROMPT_VERSION, AISignalAnalysis
from etfpulse.pipeline.detectors import DetectorHit, compute_fingerprint
from etfpulse.pipeline.regime_monitor import RegimeClassification
from etfpulse.pipeline.signal_builder import build_signal, run_daily_cycle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hit(
    *,
    signal_type: str = "flow_anomaly",
    asset: str = "BTC",
    signal_date: date = date(2026, 4, 22),
    extra: str = "v1",
) -> DetectorHit:
    """Build a synthetic DetectorHit with a deterministic, unique fingerprint."""
    return DetectorHit(
        signal_type=signal_type,
        asset=asset,
        signal_date=signal_date,
        trigger_data={"streak_length": 3, "streak_direction": "long"},
        fingerprint=compute_fingerprint(signal_type, asset, signal_date.isoformat(), extra),
    )


_VALID_ANALYSIS = AISignalAnalysis(
    headline="Test signal",
    reasoning=["r1"],
    confidence=8,
    risks=["risk1"],
    suggested_action="consider short",
    time_horizon="swing",
)


class _StubDetector:
    """In-memory detector that returns a fixed list of hits.

    Use this in `monkeypatch.setattr(signal_builder, "ALL_DETECTORS", ...)`
    to make `run_daily_cycle` deterministic.
    """

    def __init__(self, name: str, hits: list[DetectorHit]) -> None:
        self.name = name
        self.signal_type = hits[0].signal_type if hits else "flow_anomaly"
        self._hits = hits

    async def detect(self, session, **_kwargs):  # type: ignore[no-untyped-def]
        # **_kwargs absorbs current Protocol kwargs (as_of, current_regime)
        # so stub signature parity doesn't need updating every time a new
        # optional kwarg is added.
        return list(self._hits)


class _BrokenDetector:
    name = "broken"
    signal_type = "flow_anomaly"

    async def detect(self, session, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# build_signal — direct tests
# ---------------------------------------------------------------------------


class TestBuildSignal:
    async def test_inserts_with_ai_analysis(self, db_session, monkeypatch):
        """Happy path — AI returns valid analysis, all fields populated."""

        async def _ai(*args, **kwargs):
            return _VALID_ANALYSIS

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        hit = _make_hit()
        signal = await build_signal(db_session, hit)

        assert signal is not None
        assert signal.id is not None
        assert signal.signal_type == "flow_anomaly"
        assert signal.asset == "BTC"
        assert signal.signal_date == date(2026, 4, 22)
        assert signal.confidence == 8
        assert signal.ai_analysis is not None
        assert signal.ai_analysis["headline"] == "Test signal"
        assert signal.expires_at is not None
        # Default param — price fields stay NULL when the caller passes nothing.
        # The real production path (`run_daily_cycle`) threads a price through
        # via `pipeline.prices`; see `test_builds_with_price_and_source` below.
        assert signal.price_at_creation is None

    async def test_inserts_without_ai_when_analyze_returns_none(self, db_session, monkeypatch):
        """R6 — AI failure must not block signal persistence."""

        async def _ai(*args, **kwargs):
            return None

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        hit = _make_hit()
        signal = await build_signal(db_session, hit)

        assert signal is not None
        assert signal.id is not None
        assert signal.ai_analysis is None
        assert signal.confidence is None
        assert signal.expires_at is None
        # PR I.2: AI-failed signals get NULL confirmation_score by design —
        # there's no `suggested_action` to derive direction from. The delivery
        # gate already filters them via `confidence IS NULL`; confirmation
        # is the secondary gate, not a substitute.
        assert signal.confirmation_score is None
        assert signal.factor_votes is None


class TestBuildSignalConfirmation:
    """PR I.2 — confirmation score is computed post-AI when:
        - AI returned a non-`wait` action (we have a direction), AND
        - `price_source` is non-NULL (the price factor needs a provider
          to pin its klines fetch to).

    A factor-scoring failure inside `build_signal` is caught + logged;
    the signal still persists with `confirmation_score=None` rather than
    failing the whole build (same D13 catch-and-continue philosophy as the
    cycle loop).
    """

    @staticmethod
    def _stub_klines_returning(close_prices: tuple[str, str]):
        """Build a klines_fetcher stub returning two synthetic bars anchored
        to `now() - 24h` and `now()`. Anchoring to `now()` matters because
        `signal_builder` sets `Signal.created_at = now()`; the price factor
        scans bars in `[created_at - window_hours, created_at]`. Bars at
        fixed historical dates would fall outside this window and silently
        score vote=0.
        """
        from datetime import UTC, datetime, timedelta

        from etfpulse.pipeline.prices import PriceBar

        end = datetime.now(UTC)
        start = end - timedelta(hours=24)
        ts0 = int(start.timestamp() * 1000)
        ts1 = int(end.timestamp() * 1000)
        c0 = Decimal(close_prices[0])
        c1 = Decimal(close_prices[1])
        bars = [
            PriceBar(timestamp_ms=ts0, open=c0, high=c0, low=c0, close=c0),
            PriceBar(timestamp_ms=ts1, open=c1, high=c1, low=c1, close=c1),
        ]

        async def _fetch(asset, source, *, start_time_ms=None, end_time_ms=None, limit=100):
            return bars

        return _fetch

    async def test_confirmation_populated_when_price_and_regime_agree(
        self, db_session, monkeypatch
    ):
        # Long signal + 2% up move + MARKUP regime → score 2 (price + regime;
        # news is always 0 in v1). All three votes in factor_votes.
        async def _ai(*args, **kwargs):
            return AISignalAnalysis(
                headline="Long signal",
                reasoning=["r"],
                confidence=8,
                risks=["k"],
                suggested_action="consider long",
                time_horizon="swing",
            )

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)
        monkeypatch.setattr(
            "etfpulse.pipeline.factors.get_daily_klines_from_source",
            self._stub_klines_returning(("100", "102")),
        )

        regime = RegimeClassification(
            regime=MarketRegime.MARKUP,
            signal_posture=SignalPosture.NORMAL,
            confidence=7,
            reasoning={},
            macro_events_nearby=[],
            btc_dominance=None,
        )
        signal = await build_signal(
            db_session,
            _make_hit(extra="conf-2"),
            price_at_creation=Decimal("100"),
            price_source="binance",
            regime=regime,
        )
        assert signal is not None
        assert signal.confirmation_score == 2
        assert signal.factor_votes is not None
        assert set(signal.factor_votes.keys()) == {"price", "regime", "news"}
        assert signal.factor_votes["price"]["vote"] == 1
        assert signal.factor_votes["regime"]["vote"] == 1
        assert signal.factor_votes["news"]["vote"] == 0

    async def test_confirmation_null_for_wait_action(self, db_session, monkeypatch):
        # "wait" means no direction to confirm; orchestrator short-circuits
        # before scoring. Signal persists, confirmation stays NULL.
        async def _ai(*args, **kwargs):
            return AISignalAnalysis(
                headline="Wait signal",
                reasoning=["r"],
                confidence=5,
                risks=["k"],
                suggested_action="wait",
                time_horizon="swing",
            )

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        signal = await build_signal(
            db_session,
            _make_hit(extra="wait-null"),
            price_at_creation=Decimal("100"),
            price_source="binance",
        )
        assert signal is not None
        # AI persisted (the signal is real); only confirmation is NULL.
        assert signal.ai_analysis is not None
        assert signal.ai_analysis["suggested_action"] == "wait"
        assert signal.confirmation_score is None
        assert signal.factor_votes is None

    async def test_confirmation_null_when_price_source_missing(self, db_session, monkeypatch):
        # Defensive — when both price providers failed at cycle start the
        # caller passes `price_source=None`. Without a pinned source we
        # can't honestly score the price factor, so we skip scoring
        # entirely. Signal persists; confirmation NULL.
        async def _ai(*args, **kwargs):
            return AISignalAnalysis(
                headline="No source",
                reasoning=["r"],
                confidence=7,
                risks=["k"],
                suggested_action="consider long",
                time_horizon="swing",
            )

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        signal = await build_signal(
            db_session,
            _make_hit(extra="no-source"),
            price_at_creation=None,
            price_source=None,
        )
        assert signal is not None
        assert signal.confirmation_score is None
        assert signal.factor_votes is None

    async def test_confirmation_null_for_unsupported_asset(self, db_session, monkeypatch):
        # PR F.3 — MARKET sentinel signals don't have a single asset to
        # price against. The build_signal guard reads `hit.asset in ("BTC",
        # "ETH")` and skips scoring otherwise. Future intra-asset MARKET
        # logic would need its own factor design.
        async def _ai(*args, **kwargs):
            return AISignalAnalysis(
                headline="Market sentinel",
                reasoning=["r"],
                confidence=6,
                risks=["k"],
                suggested_action="consider long",
                time_horizon="swing",
            )

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        # Override the hit's asset to MARKET — same shape regime_shift
        # signals carry post-PR-F.3.
        hit = DetectorHit(
            signal_type="regime_shift",
            asset="MARKET",
            signal_date=date(2026, 4, 22),
            trigger_data={},
            fingerprint=compute_fingerprint("regime_shift", "MARKET", "2026-04-22", "conf"),
        )
        signal = await build_signal(
            db_session,
            hit,
            price_at_creation=Decimal("100"),
            price_source="binance",
        )
        assert signal is not None
        assert signal.confirmation_score is None
        assert signal.factor_votes is None

    async def test_factor_failure_does_not_abort_signal_build(self, db_session, monkeypatch):
        # D13-style: factor scoring is a non-fatal step. A buggy/throwing
        # factor scorer must not abort the signal build — log + persist
        # confirmation NULL, move on.
        async def _ai(*args, **kwargs):
            return AISignalAnalysis(
                headline="Boom",
                reasoning=["r"],
                confidence=7,
                risks=["k"],
                suggested_action="consider long",
                time_horizon="swing",
            )

        async def _explode(*args, **kwargs):
            raise RuntimeError("simulated factor failure")

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)
        # Patch at the factor module itself — the shared helper in
        # `pipeline.analysis` resolves the import lazily on each call.
        monkeypatch.setattr(
            "etfpulse.pipeline.factors.compute_confirmation",
            _explode,
        )

        signal = await build_signal(
            db_session,
            _make_hit(extra="factor-boom"),
            price_at_creation=Decimal("100"),
            price_source="binance",
        )
        # Signal persisted; AI analysis attached; confirmation NULL
        # because the scorer raised.
        assert signal is not None
        assert signal.ai_analysis is not None
        assert signal.confirmation_score is None
        assert signal.factor_votes is None

    async def test_idempotent_returns_none_on_duplicate(self, db_session, monkeypatch):
        """Second insert of same fingerprint+date is a silent no-op."""

        async def _ai(*args, **kwargs):
            return _VALID_ANALYSIS

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        hit = _make_hit()
        first = await build_signal(db_session, hit)
        second = await build_signal(db_session, hit)

        assert first is not None
        assert second is None
        # Only one row exists in the table.
        rows = (await db_session.execute(select(Signal))).scalars().all()
        assert len(rows) == 1

    async def test_idempotent_skip_does_not_call_ai(self, db_session, monkeypatch):
        """D12 — re-runs of the same hit must NOT spend an OpenRouter call."""
        call_count = 0

        async def _ai(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _VALID_ANALYSIS

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        hit = _make_hit()
        await build_signal(db_session, hit)
        await build_signal(db_session, hit)

        assert call_count == 1, "AI must be called exactly once across two identical hits"

    async def test_different_signal_date_creates_separate_row(self, db_session, monkeypatch):
        """The unique index includes signal_date — same fingerprint on a
        new date is a different signal."""

        async def _ai(*args, **kwargs):
            return _VALID_ANALYSIS

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        # Same fingerprint via same `extra`, but different signal_date.
        hit_a = _make_hit(signal_date=date(2026, 4, 22), extra="same")
        hit_b = _make_hit(signal_date=date(2026, 4, 23), extra="same")
        # Sanity: fingerprints DO match (we built them with the same extras
        # but different signal_dates means the date is what disambiguates).
        # Actually our fingerprint helper includes signal_date in the hash —
        # check that, then make a hit pair with the SAME fingerprint to
        # really test the (fingerprint, signal_date) compound uniqueness.
        forced_fp = compute_fingerprint("force", "same")
        hit_a = DetectorHit(
            signal_type="flow_anomaly",
            asset="BTC",
            signal_date=date(2026, 4, 22),
            trigger_data={},
            fingerprint=forced_fp,
        )
        hit_b = DetectorHit(
            signal_type="flow_anomaly",
            asset="BTC",
            signal_date=date(2026, 4, 23),
            trigger_data={},
            fingerprint=forced_fp,
        )

        a = await build_signal(db_session, hit_a)
        b = await build_signal(db_session, hit_b)

        assert a is not None and b is not None
        assert a.id != b.id
        rows = (await db_session.execute(select(Signal))).scalars().all()
        assert len(rows) == 2

    async def test_builds_with_price_and_source(self, db_session, monkeypatch, stub_ai):
        """Issue #34 + Stage 07-P3: caller passes price + source → row stores
        both as first-class columns.

        Price lands on `Signal.price_at_creation` (typed Numeric, Decimal in
        Python). Source lands on the dedicated `Signal.price_source` String
        column (Stage 07 migration). `trigger_data` MUST NOT carry a
        `price_source` key — the column is the only source of truth now.
        """
        hit = _make_hit()
        signal = await build_signal(
            db_session,
            hit,
            price_at_creation=Decimal("84120.50"),
            price_source="sosovalue",
        )
        assert signal is not None
        assert signal.price_at_creation == Decimal("84120.50")
        assert signal.price_source == "sosovalue"
        # trigger_data must stay as the detector produced it — no leakage.
        assert "price_source" not in signal.trigger_data
        for key in hit.trigger_data:
            assert signal.trigger_data[key] == hit.trigger_data[key]

    async def test_builds_without_price_leaves_fields_null(self, db_session, monkeypatch, stub_ai):
        """When both price providers fail, the signal still persists — with
        NULL `price_at_creation` and NULL `price_source`. The backfill
        script (scripts/backfill_signal_prices.py) is responsible for
        revisiting these rows later."""
        hit = _make_hit()
        signal = await build_signal(db_session, hit)  # both price args default None
        assert signal is not None
        assert signal.price_at_creation is None
        assert signal.price_source is None
        assert "price_source" not in signal.trigger_data

    async def test_stamps_ai_prompt_version_on_insert(self, db_session, monkeypatch, stub_ai):
        """Stage 7-P6 (closes #32): every new Signal row must carry the
        current `AI_PROMPT_VERSION` so track-record queries can compare
        apples-to-apples across prompt revisions."""
        hit = _make_hit()
        signal = await build_signal(db_session, hit)
        assert signal is not None
        assert signal.ai_prompt_version == AI_PROMPT_VERSION

    async def test_mirrors_price_levels_to_signal_columns(self, db_session, monkeypatch):
        """Stage 8-P1 — when AI returns entry/stop/target, build_signal
        must copy them onto Signal.entry_price/stop_price/target_price
        columns (not just leave them in ai_analysis JSONB). Outcome
        evaluator reads the columns; JSONB is the audit trail."""

        async def _ai(*args, **kwargs):
            return AISignalAnalysis(
                headline="Test",
                reasoning=["r"],
                confidence=8,
                risks=["risk"],
                suggested_action="consider long",
                time_horizon="swing",
                entry_price=Decimal("84200.00"),
                stop_price=Decimal("82000.00"),
                target_price=Decimal("89500.00"),
            )

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        hit = _make_hit()
        signal = await build_signal(db_session, hit)

        assert signal is not None
        assert signal.entry_price == Decimal("84200.00")
        assert signal.stop_price == Decimal("82000.00")
        assert signal.target_price == Decimal("89500.00")
        # Audit trail in JSONB carries the same values (as strings — Decimal
        # serializes that way in mode="json").
        assert signal.ai_analysis is not None
        assert signal.ai_analysis["entry_price"] == "84200.00"
        assert signal.ai_analysis["stop_price"] == "82000.00"
        assert signal.ai_analysis["target_price"] == "89500.00"

    async def test_wait_suggestion_leaves_price_columns_null(self, db_session, monkeypatch):
        """A 'wait' suggestion is the AI declining to recommend a trade —
        the validator drops any volunteered prices, so the columns must
        also be NULL on the persisted Signal."""

        async def _ai(*args, **kwargs):
            return AISignalAnalysis(
                headline="Wait for confirmation",
                reasoning=["r"],
                confidence=4,
                risks=["risk"],
                suggested_action="wait",
                time_horizon="scalp",
                # The schema-level model_validator will null these out
                # regardless of what we pass — verify the column propagation.
                entry_price=Decimal("84200"),
                stop_price=Decimal("82000"),
                target_price=Decimal("89500"),
            )

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        signal = await build_signal(db_session, _make_hit())
        assert signal is not None
        assert signal.entry_price is None
        assert signal.stop_price is None
        assert signal.target_price is None

    async def test_ai_failure_leaves_price_columns_null(self, db_session, monkeypatch):
        """When AI returns None, the columns stay NULL (no fall-back to
        trigger_data values — those aren't AI-suggested levels)."""

        async def _ai(*args, **kwargs):
            return None

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        signal = await build_signal(db_session, _make_hit())
        assert signal is not None
        assert signal.entry_price is None
        assert signal.stop_price is None
        assert signal.target_price is None

    async def test_regime_and_news_context_persist_to_trigger_data(
        self, db_session, monkeypatch, stub_ai
    ):
        """When the orchestrator passes `regime` + `news_context`, both land
        inside `trigger_data` JSONB under known keys for audit + future
        re-analysis. Original DetectorHit keys are preserved alongside."""
        regime = RegimeClassification(
            regime=MarketRegime.MARKUP,
            signal_posture=SignalPosture.NORMAL,
            confidence=8,
            reasoning={"score": 50},
            macro_events_nearby=["FOMC"],
        )
        news = [
            {"title": "BlackRock filing", "category": 3, "published_iso": "x", "summary": "y"},
        ]

        hit = _make_hit()
        signal = await build_signal(db_session, hit, regime=regime, news_context=news)

        assert signal is not None
        assert signal.trigger_data["regime_at_creation"]["regime"] == "markup"
        assert signal.trigger_data["regime_at_creation"]["signal_posture"] == "normal"
        assert signal.trigger_data["regime_at_creation"]["confidence"] == 8
        assert signal.trigger_data["regime_at_creation"]["macro_events_nearby"] == ["FOMC"]
        assert signal.trigger_data["news_context"] == news
        # Original DetectorHit keys preserved.
        for key in hit.trigger_data:
            assert signal.trigger_data[key] == hit.trigger_data[key]

    async def test_regime_at_creation_persists_reasoning_and_btc_dominance(
        self, db_session, stub_ai
    ):
        """Issue #59 — `reasoning` (dict) and `btc_dominance` (Decimal → str)
        must round-trip through the JSONB blob so `ai_backfill` reconstructs
        the same prompt context the fresh build saw. Verifies BOTH the
        presence of the keys AND that `btc_dominance` is stringified —
        a raw Decimal would either (a) crash asyncpg JSONB serialization,
        or (b) be coerced by the JSON encoder in ways that confuse the
        reader's `isinstance(str)` check downstream."""
        regime = RegimeClassification(
            regime=MarketRegime.MARKUP,
            signal_posture=SignalPosture.NORMAL,
            confidence=8,
            reasoning={
                "flow": {"score": 6, "note": "BTC streak broken"},
                "dominance": {"available": True, "btc_dominance": "54.32"},
            },
            macro_events_nearby=["FOMC"],
            btc_dominance=Decimal("54.3200"),
        )

        signal = await build_signal(db_session, _make_hit(), regime=regime)

        assert signal is not None
        persisted = signal.trigger_data["regime_at_creation"]
        # The pre-existing 4 keys still land correctly (regression guard).
        assert persisted["regime"] == "markup"
        assert persisted["signal_posture"] == "normal"
        assert persisted["confidence"] == 8
        assert persisted["macro_events_nearby"] == ["FOMC"]
        # New keys land with the right shapes.
        assert persisted["reasoning"] == {
            "flow": {"score": 6, "note": "BTC streak broken"},
            "dominance": {"available": True, "btc_dominance": "54.32"},
        }
        # Pin the writer contract: btc_dominance MUST be a str, not a Decimal.
        # If a future refactor strips the `str(...)` wrap, this test fires
        # before the reader silently degrades to None (it requires str input).
        assert isinstance(persisted["btc_dominance"], str)
        assert persisted["btc_dominance"] == "54.3200"

    async def test_regime_at_creation_persists_null_btc_dominance(self, db_session, stub_ai):
        """When the regime classifier returned `btc_dominance=None`
        (SoSoValue spotlight API down), the persisted value is JSON null —
        NOT the literal string "None". `_reconstruct_regime`'s
        `isinstance(str)` guard correctly treats None as 'no btc_dominance'."""
        regime = RegimeClassification(
            regime=MarketRegime.UNCERTAIN,
            signal_posture=SignalPosture.CAUTIOUS,
            confidence=4,
            reasoning={"dominance": {"available": False, "fetch_error": "timeout"}},
            macro_events_nearby=[],
            btc_dominance=None,
        )

        signal = await build_signal(db_session, _make_hit(), regime=regime)

        assert signal is not None
        persisted = signal.trigger_data["regime_at_creation"]
        assert persisted["btc_dominance"] is None  # JSON null, not "None"

    async def test_regime_and_news_context_forwarded_to_ai(self, db_session, monkeypatch):
        """`build_signal` must forward `regime` + `news_context` to
        `openrouter_client.analyze` so the AI prompt sees them. Verified by
        capturing the kwargs the stub receives."""
        captured_kwargs: dict = {}

        async def _ai(**kwargs):
            captured_kwargs.update(kwargs)
            return AISignalAnalysis(
                headline="x",
                reasoning=["r"],
                confidence=7,
                risks=["k"],
                suggested_action="consider long",
                time_horizon="swing",
            )

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        regime = RegimeClassification(
            regime=MarketRegime.ACCUMULATION,
            signal_posture=SignalPosture.AGGRESSIVE,
            confidence=6,
            reasoning={},
            macro_events_nearby=[],
        )
        news = [{"title": "n", "category": 3, "published_iso": "x", "summary": None}]

        hit = _make_hit()
        await build_signal(db_session, hit, regime=regime, news_context=news)

        assert captured_kwargs["regime"] is regime
        assert captured_kwargs["news_context"] == news

    async def test_price_at_creation_forwarded_as_current_price(self, db_session, monkeypatch):
        """Stage 8-P1 — the spot price we already fetched for
        `Signal.price_at_creation` must be threaded through to the AI as
        `current_price` so the v3 entry/stop/target rules have a real
        anchor instead of guessing from the model's training data."""
        captured_kwargs: dict = {}

        async def _ai(**kwargs):
            captured_kwargs.update(kwargs)
            return AISignalAnalysis(
                headline="x",
                reasoning=["r"],
                confidence=7,
                risks=["k"],
                suggested_action="consider long",
                time_horizon="swing",
            )

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        await build_signal(
            db_session,
            _make_hit(),
            price_at_creation=Decimal("84250.50"),
            price_source="binance",
        )

        assert captured_kwargs["current_price"] == Decimal("84250.50")

    async def test_no_price_means_current_price_is_none(self, db_session, monkeypatch):
        """When both price providers failed (`price_at_creation=None`), the
        AI receives current_price=None and the v3 system prompt instructs
        it to return null entry/stop/target rather than guess."""
        captured_kwargs: dict = {}

        async def _ai(**kwargs):
            captured_kwargs.update(kwargs)
            return AISignalAnalysis(
                headline="x",
                reasoning=["r"],
                confidence=7,
                risks=["k"],
                suggested_action="consider long",
                time_horizon="swing",
            )

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)

        await build_signal(db_session, _make_hit())  # default price_at_creation=None

        assert captured_kwargs["current_price"] is None


# ---------------------------------------------------------------------------
# run_daily_cycle — orchestrator tests
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_ai(monkeypatch):
    """Make OpenRouter return a valid analysis for every call."""

    async def _ai(*args, **kwargs):
        return _VALID_ANALYSIS

    monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai)


class TestRunDailyCycle:
    async def test_first_call_inserts_hits(self, db_session, monkeypatch, stub_ai):
        """Case (a): ALL_DETECTORS yields hits → signals_new > 0."""
        stub = _StubDetector(
            "stub",
            [_make_hit(asset="BTC", extra="run1"), _make_hit(asset="ETH", extra="run1")],
        )
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [stub])

        summary = await run_daily_cycle(db_session)

        assert summary["signals_new"] == 2
        assert summary["signals_duplicate"] == 0
        assert summary["detector_errors"] == []
        rows = (await db_session.execute(select(Signal))).scalars().all()
        assert len(rows) == 2

    async def test_idempotent_rerun_yields_zero_new(self, db_session, monkeypatch, stub_ai):
        """Case (b): same cycle run twice → second run reports 0 new, all duplicate."""
        stub = _StubDetector("stub", [_make_hit(extra="run2")])
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [stub])

        first = await run_daily_cycle(db_session)
        second = await run_daily_cycle(db_session)

        assert first["signals_new"] == 1
        assert second["signals_new"] == 0
        assert second["signals_duplicate"] == 1

    async def test_ai_failure_persists_signal_with_null_analysis(self, db_session, monkeypatch):
        """Case (c): AI returns None → signal exists, ai_analysis is NULL."""

        async def _ai_none(*args, **kwargs):
            return None

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.openrouter_client.analyze", _ai_none)
        stub = _StubDetector("stub", [_make_hit(extra="run3")])
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [stub])

        summary = await run_daily_cycle(db_session)

        assert summary["signals_new"] == 1
        assert summary["ai_succeeded"] == 0
        assert summary["ai_failed"] == 1
        signal = (await db_session.execute(select(Signal))).scalar_one()
        assert signal.ai_analysis is None
        assert signal.confidence is None

    async def test_different_signal_date_yields_separate_rows(
        self, db_session, monkeypatch, stub_ai
    ):
        """Case (d): two stubs hitting different signal_dates produce two rows."""
        stub1 = _StubDetector("stub1", [_make_hit(signal_date=date(2026, 4, 22), extra="d1")])
        stub2 = _StubDetector("stub2", [_make_hit(signal_date=date(2026, 4, 23), extra="d2")])
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [stub1, stub2])

        summary = await run_daily_cycle(db_session)

        assert summary["signals_new"] == 2
        rows = (await db_session.execute(select(Signal))).scalars().all()
        dates = {r.signal_date for r in rows}
        assert dates == {date(2026, 4, 22), date(2026, 4, 23)}

    async def test_detector_exception_does_not_kill_cycle(self, db_session, monkeypatch, stub_ai):
        """Case (e): one detector raises, the next still runs (D13)."""
        broken = _BrokenDetector()
        good = _StubDetector("good", [_make_hit(extra="surv")])
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [broken, good])

        summary = await run_daily_cycle(db_session)

        assert summary["detector_errors"] == [("broken", "RuntimeError")]
        assert summary["signals_new"] == 1
        # The good detector's hit still landed.
        rows = (await db_session.execute(select(Signal))).scalars().all()
        assert len(rows) == 1

    async def test_skip_flag_short_circuits_when_no_new_data(
        self, db_session, monkeypatch, stub_ai
    ):
        """Branch 3 — intra-day cycles pass `skip_if_no_new_data=True`. On
        a second run against the same upstream data, ingest_* return 0 new
        rows, the cycle short-circuits BEFORE detectors run, and the
        summary signals the skip.

        Detector that would otherwise fire on every call is monkey-patched
        in so we can prove it was NOT called on the skipped run."""
        call_count = {"n": 0}

        class _CountingDetector:
            name = "counter"
            signal_type = "flow_anomaly"

            async def detect(self, session, **_kwargs):
                call_count["n"] += 1
                return []

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [_CountingDetector()])

        # First run primes the DB (fixtures populate ETF + news).
        first = await run_daily_cycle(db_session, skip_if_no_new_data=True)
        assert first["skipped"] is False
        assert call_count["n"] == 1  # detector ran

        # Second run sees zero new rows (idempotent ingest); skip should fire.
        second = await run_daily_cycle(db_session, skip_if_no_new_data=True)
        assert second["skipped"] is True
        # Detector was NOT called the second time — that's the waste we
        # save by skipping. Counter stays at 1.
        assert call_count["n"] == 1
        # Skip happens AFTER ingestion, so ingest summaries are still
        # populated (with zero counts).
        assert sum(second["ingested"].values()) == 0
        assert sum(second["news_ingested"].values()) == 0

    async def test_skip_flag_continues_when_new_data_arrives(
        self, db_session, monkeypatch, stub_ai
    ):
        """Symmetric: when new data IS ingested, the skip-guard does NOT
        fire — detectors run normally. Proves the skip is conditional on
        no-new-data, not unconditional."""
        ran = {"n": 0}

        class _CountingDetector:
            name = "counter2"
            signal_type = "flow_anomaly"

            async def detect(self, session, **_kwargs):
                ran["n"] += 1
                return []

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [_CountingDetector()])

        summary = await run_daily_cycle(db_session, skip_if_no_new_data=True)

        # Fixtures landed new rows on this first call.
        assert summary["skipped"] is False
        assert ran["n"] == 1
        assert sum(summary["ingested"].values()) > 0

    async def test_default_skip_flag_false_always_runs(self, db_session, monkeypatch, stub_ai):
        """Daily cron + admin trigger pass `skip_if_no_new_data=False`
        (the default). Even after ingest returns 0 new rows, detectors
        still execute — operators expect "fire the cycle now" semantics."""
        ran = {"n": 0}

        class _CountingDetector:
            name = "counter3"
            signal_type = "flow_anomaly"

            async def detect(self, session, **_kwargs):
                ran["n"] += 1
                return []

        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [_CountingDetector()])

        await run_daily_cycle(db_session)  # primes DB
        second = await run_daily_cycle(db_session)  # default skip=False

        assert second["skipped"] is False
        assert ran["n"] == 2  # detector ran on BOTH calls

    async def test_summary_shape(self, db_session, monkeypatch, stub_ai):
        """The returned dict has the documented keys — protects #47/#50 callers."""
        monkeypatch.setattr("etfpulse.pipeline.signal_builder.ALL_DETECTORS", [])

        summary = await run_daily_cycle(db_session)

        expected_keys = {
            "ingested",
            "ingest_errors",
            "news_ingested",
            "news_errors",
            "prices",
            "price_errors",
            "regime",
            "regime_error",
            "detectors_run",
            "detector_errors",
            "signals_new",
            "signals_duplicate",
            "ai_succeeded",
            "ai_failed",
            # Branch 3 — `True` when the skip-guard short-circuited.
            "skipped",
        }
        assert set(summary.keys()) == expected_keys
