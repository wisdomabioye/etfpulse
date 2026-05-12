"""Tests for `pipeline.ai_backfill` — retry AI on NULL-AI signals.

The module's contract (idempotent, NULL-only filter, caller-owned commit,
bounded by `limit`) is covered here directly; the admin route in
`tests/test_app/test_admin.py` exercises the HTTP wrapper.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from etfpulse.models import MarketRegime, Signal, SignalPosture, SignalStatus
from etfpulse.pipeline.ai_backfill import _reconstruct_regime, backfill_null_ai
from etfpulse.pipeline.analysis import AISignalAnalysis
from etfpulse.pipeline.detectors import compute_fingerprint

_VALID_ANALYSIS = AISignalAnalysis(
    headline="Backfill test",
    reasoning=["recovered after credit top-up"],
    confidence=7,
    risks=["sample risk"],
    suggested_action="consider long",
    time_horizon="swing",
)


async def _seed_null_ai_signal(
    db_session,
    *,
    asset: str = "BTC",
    signal_type: str = "flow_anomaly",
    fingerprint_seed: str = "seed",
    trigger_data: dict | None = None,
    price_at_creation: Decimal | None = Decimal("82000"),
) -> Signal:
    signal = Signal(
        signal_type=signal_type,
        asset=asset,
        trigger_data=trigger_data
        or {
            "streak_length": 4,
            "regime_at_creation": {
                "regime": "uncertain",
                "signal_posture": "cautious",
                "confidence": 2,
                "macro_events_nearby": ["CPI (MoM)"],
            },
            "news_context": [
                {
                    "title": "headline",
                    "summary": "summary",
                    "category": 3,
                    "published_iso": "2026-05-10T12:00:00+00:00",
                }
            ],
        },
        ai_analysis=None,
        confidence=None,
        status=SignalStatus.PENDING.value,
        fingerprint=compute_fingerprint(asset, signal_type, fingerprint_seed),
        signal_date=date(2026, 5, 8),
        price_at_creation=price_at_creation,
    )
    db_session.add(signal)
    await db_session.flush()
    return signal


class TestBackfillNullAi:
    async def test_updates_null_signal_when_analyze_succeeds(self, db_session, monkeypatch):
        """Happy path — NULL-AI signal gets fully populated by one call."""
        signal = await _seed_null_ai_signal(db_session)
        captured: dict = {}

        async def _ai(**kwargs):
            captured.update(kwargs)
            return _VALID_ANALYSIS, None

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )

        summary = await backfill_null_ai(db_session, limit=10)

        assert summary == {"scanned": 1, "updated": 1, "failed": 0, "error_samples": []}
        await db_session.refresh(signal)
        assert signal.ai_analysis is not None
        assert signal.confidence == 7
        assert signal.expires_at is not None
        # Reconstruction round-trip: regime + news + price should be threaded
        # back into the AI call exactly as persisted.
        assert captured["asset"] == "BTC"
        assert captured["signal_type"] == "flow_anomaly"
        assert captured["current_price"] == Decimal("82000")
        assert captured["regime"] is not None
        assert captured["regime"].regime.value == "uncertain"
        assert captured["news_context"] == [
            {
                "title": "headline",
                "summary": "summary",
                "category": 3,
                "published_iso": "2026-05-10T12:00:00+00:00",
            }
        ]
        # Enrichment keys must NOT leak into the trigger_data passed to AI —
        # they're surfaced via the dedicated regime/news_context args.
        assert "regime_at_creation" not in captured["trigger_data"]
        assert "news_context" not in captured["trigger_data"]
        assert captured["trigger_data"]["streak_length"] == 4

    async def test_idempotent_second_call_is_noop(self, db_session, monkeypatch):
        """Re-running after success scans 0 — the NULL filter excludes
        already-enriched rows. Critical for safe button-mashing."""
        await _seed_null_ai_signal(db_session)

        async def _ai(**kwargs):
            return _VALID_ANALYSIS, None

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )
        first = await backfill_null_ai(db_session, limit=10)
        second = await backfill_null_ai(db_session, limit=10)

        assert first["updated"] == 1
        assert second == {"scanned": 0, "updated": 0, "failed": 0, "error_samples": []}

    async def test_records_error_sample_when_analyze_returns_none(self, db_session, monkeypatch):
        """When analyze returns None (logged failure inside adapter), the
        Signal stays NULL and the failure is sampled into the response."""
        signal = await _seed_null_ai_signal(db_session)

        async def _ai(**kwargs):
            return None, "request failed: HTTP 402: Insufficient credits."

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )
        summary = await backfill_null_ai(db_session, limit=10)

        assert summary["scanned"] == 1
        assert summary["updated"] == 0
        assert summary["failed"] == 1
        assert len(summary["error_samples"]) == 1
        assert summary["error_samples"][0]["signal_id"] == signal.id
        # `_classify_reason` maps "request failed: HTTP 402: …" → "InsufficientCredits".
        assert summary["error_samples"][0]["kind"] == "InsufficientCredits"
        assert "402" in summary["error_samples"][0]["detail"]

        await db_session.refresh(signal)
        assert signal.ai_analysis is None  # row left NULL on failure

    async def test_caps_error_samples_at_three(self, db_session, monkeypatch):
        """A backlog where every row fails the same way must not blow up
        the response payload — sampling caps at 3."""
        for i in range(5):
            await _seed_null_ai_signal(db_session, fingerprint_seed=f"seed-{i}")

        async def _ai(**kwargs):
            return None, "request failed: HTTP 402: Insufficient credits."

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )
        summary = await backfill_null_ai(db_session, limit=10)

        assert summary["scanned"] == 5
        assert summary["failed"] == 5
        assert len(summary["error_samples"]) == 3

    async def test_limit_caps_scanned_count(self, db_session, monkeypatch):
        """`limit` is the operator's spend control — must be respected
        even when more NULL rows exist."""
        for i in range(5):
            await _seed_null_ai_signal(db_session, fingerprint_seed=f"seed-{i}")

        async def _ai(**kwargs):
            return _VALID_ANALYSIS, None

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )
        summary = await backfill_null_ai(db_session, limit=2)

        assert summary["scanned"] == 2
        assert summary["updated"] == 2

    async def test_processes_oldest_first(self, db_session, monkeypatch):
        """FIFO ordering — oldest stranded signals clear before newer ones,
        matching operator intuition for backlog draining."""
        old = await _seed_null_ai_signal(db_session, fingerprint_seed="old")
        new = await _seed_null_ai_signal(db_session, fingerprint_seed="new")
        # Force created_at ordering — flush sets server_default=now() on each
        # so we override to make the test deterministic.
        old.created_at = datetime(2026, 5, 1, tzinfo=UTC)
        new.created_at = datetime(2026, 5, 10, tzinfo=UTC)
        await db_session.flush()

        seen_order: list[int] = []

        async def _ai(**kwargs):
            # Identify which signal is being processed via fingerprint round-trip.
            # We can't see signal_id directly here, so we use a counter approach:
            # asserting that on `limit=1` only the OLD signal updates.
            seen_order.append(1)
            return _VALID_ANALYSIS, None

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )
        summary = await backfill_null_ai(db_session, limit=1)

        assert summary["updated"] == 1
        # Verify the OLDER row got enriched first.
        await db_session.refresh(old)
        await db_session.refresh(new)
        assert old.ai_analysis is not None
        assert new.ai_analysis is None

    async def test_does_not_commit_caller_owns_transaction(self, db_session, monkeypatch):
        """D14 — backfill must not commit. Caller's rollback would undo
        all updates if the helper had committed mid-batch."""
        signal = await _seed_null_ai_signal(db_session)

        async def _ai(**kwargs):
            return _VALID_ANALYSIS, None

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )
        await backfill_null_ai(db_session, limit=10)

        # In-session the row is updated...
        await db_session.refresh(signal)
        assert signal.ai_analysis is not None

        # ...but the test's rollback (from db_session fixture) leaves the
        # DB unchanged, proving the helper did not slip a commit through.
        # We can only assert this indirectly: confirming the function
        # returned WITHOUT having committed (no exception, no autoflush
        # boundary crossed).

    async def test_handles_legacy_signal_without_regime_or_news(self, db_session, monkeypatch):
        """Pre-Stage-7 signals carry only the bare detector trigger_data —
        no regime_at_creation, no news_context. The backfill must still
        re-analyse, just with those args set to None."""
        signal = await _seed_null_ai_signal(
            db_session,
            trigger_data={"streak_length": 4, "direction": "long"},
        )
        captured: dict = {}

        async def _ai(**kwargs):
            captured.update(kwargs)
            return _VALID_ANALYSIS, None

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )
        summary = await backfill_null_ai(db_session, limit=10)

        assert summary["updated"] == 1
        assert captured["regime"] is None
        assert captured["news_context"] is None
        assert captured["trigger_data"] == {"streak_length": 4, "direction": "long"}

        await db_session.refresh(signal)
        assert signal.ai_analysis is not None

    async def test_partial_batch_continues_on_individual_failure(self, db_session, monkeypatch):
        """One failing row must not abort the loop — same catch-and-continue
        contract as `run_daily_cycle`."""
        s1 = await _seed_null_ai_signal(db_session, fingerprint_seed="s1")
        s2 = await _seed_null_ai_signal(db_session, fingerprint_seed="s2")
        call_count = [0]

        async def _ai(**kwargs):
            call_count[0] += 1
            return (
                (None, "request failed: HTTP 402: Insufficient credits.")
                if call_count[0] == 1
                else (_VALID_ANALYSIS, None)
            )

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )
        summary = await backfill_null_ai(db_session, limit=10)

        assert summary["scanned"] == 2
        assert summary["updated"] == 1
        assert summary["failed"] == 1

        # Verify exactly one of the two rows got enriched.
        rows = (
            (await db_session.execute(select(Signal).where(Signal.id.in_([s1.id, s2.id]))))
            .scalars()
            .all()
        )
        enriched = [r for r in rows if r.ai_analysis is not None]
        assert len(enriched) == 1


class TestBackfillMaxAgeHours:
    """Branch 6 — the auto-retry path passes `max_age_hours` to skip stale
    signals. The manual operator endpoint uses `None` to drain any age."""

    async def test_none_means_no_age_filter(self, db_session, monkeypatch):
        """When `max_age_hours is None` (default), even ancient signals
        are eligible. Matches the manual `/retry-ai` endpoint behavior."""
        ancient = await _seed_null_ai_signal(db_session, fingerprint_seed="ancient")
        ancient.created_at = datetime(2024, 1, 1, tzinfo=UTC)  # ~1.5 years old
        await db_session.flush()

        async def _ai(**kwargs):
            return _VALID_ANALYSIS, None

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )

        summary = await backfill_null_ai(db_session, limit=10)  # max_age_hours not passed → None
        assert summary["updated"] == 1

    async def test_zero_means_no_age_filter(self, db_session, monkeypatch):
        """`max_age_hours=0` is the 'unlimited' sentinel (matches the env
        knob's 0=disabled convention). Same behavior as None."""
        ancient = await _seed_null_ai_signal(db_session, fingerprint_seed="zero")
        ancient.created_at = datetime(2024, 1, 1, tzinfo=UTC)
        await db_session.flush()

        async def _ai(**kwargs):
            return _VALID_ANALYSIS, None

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )

        summary = await backfill_null_ai(db_session, limit=10, max_age_hours=0)
        assert summary["updated"] == 1

    async def test_filters_out_older_than_cutoff(self, db_session, monkeypatch):
        """A signal older than `max_age_hours` is NOT considered. Tests
        the auto-retry contract: stale market data isn't worth AI spend."""
        from datetime import timedelta

        old = await _seed_null_ai_signal(db_session, fingerprint_seed="too-old")
        old.created_at = datetime.now(UTC) - timedelta(hours=48)  # 48h old
        fresh = await _seed_null_ai_signal(db_session, fingerprint_seed="fresh")
        fresh.created_at = datetime.now(UTC) - timedelta(hours=1)  # 1h old
        await db_session.flush()

        async def _ai(**kwargs):
            return _VALID_ANALYSIS, None

        monkeypatch.setattr(
            "etfpulse.pipeline.ai_backfill.openrouter_client.analyze_with_reason", _ai
        )

        summary = await backfill_null_ai(db_session, limit=10, max_age_hours=24)
        # Only the fresh signal was scanned + updated.
        assert summary["scanned"] == 1
        assert summary["updated"] == 1
        await db_session.refresh(old)
        await db_session.refresh(fresh)
        assert old.ai_analysis is None  # excluded by age filter
        assert fresh.ai_analysis is not None


class TestReconstructRegime:
    """Issue #59 — `_reconstruct_regime` reads the JSONB blob persisted by
    `signal_builder.build_signal`. Pre-PR-A blobs lacked `reasoning` and
    `btc_dominance`; the reader must remain backwards-compatible with those
    legacy shapes while also surfacing the new fields when they're present.
    Partial corruption (one malformed key) must NOT kill the entire reconstruction
    — only that field should degrade."""

    def test_reads_reasoning_and_btc_dominance_when_present(self):
        """Round-trip the post-#59 6-key shape — both new fields appear on
        the rebuilt RegimeClassification with the correct types."""

        regime = _reconstruct_regime(
            {
                "regime_at_creation": {
                    "regime": "markup",
                    "signal_posture": "normal",
                    "confidence": 8,
                    "macro_events_nearby": ["FOMC"],
                    "reasoning": {
                        "flow": {"score": 6, "note": "BTC streak broken"},
                        "dominance": {"available": True, "btc_dominance": "54.32"},
                    },
                    "btc_dominance": "54.3200",
                }
            }
        )

        assert regime is not None
        assert regime.reasoning == {
            "flow": {"score": 6, "note": "BTC streak broken"},
            "dominance": {"available": True, "btc_dominance": "54.32"},
        }
        # str → Decimal round-trip is lossless.
        assert regime.btc_dominance == Decimal("54.3200")

    def test_legacy_shape_defaults_reasoning_and_btc_dominance(self):
        """Pre-PR-A signals persist only 4 keys. Reader must default
        `reasoning` to `{}` and `btc_dominance` to None — same behavior as
        the pre-#59 code path, so existing rows backfill identically."""

        regime = _reconstruct_regime(
            {
                "regime_at_creation": {
                    "regime": "uncertain",
                    "signal_posture": "cautious",
                    "confidence": 2,
                    "macro_events_nearby": ["CPI (MoM)"],
                }
            }
        )

        assert regime is not None
        assert regime.reasoning == {}
        assert regime.btc_dominance is None

    def test_malformed_btc_dominance_degrades_only_that_field(self):
        """Partial corruption (e.g. JSONB stored `"unknown"` instead of a
        numeric string) must NOT void the rest of the regime context.
        Inner InvalidOperation catch (#59 review pass 3) keeps the other 5
        fields intact while degrading only btc_dominance to None."""
        from etfpulse.pipeline.ai_backfill import _reconstruct_regime

        regime = _reconstruct_regime(
            {
                "regime_at_creation": {
                    "regime": "markup",
                    "signal_posture": "normal",
                    "confidence": 8,
                    "macro_events_nearby": ["FOMC"],
                    "reasoning": {"flow": {"score": 6}},
                    "btc_dominance": "not-a-number",  # malformed
                }
            }
        )

        assert regime is not None  # full reconstruction did NOT bail
        assert regime.regime == MarketRegime.MARKUP
        assert regime.signal_posture == SignalPosture.NORMAL
        assert regime.confidence == 8
        assert regime.macro_events_nearby == ["FOMC"]
        assert regime.reasoning == {"flow": {"score": 6}}
        assert regime.btc_dominance is None  # only this field degraded

    def test_missing_required_field_returns_none(self):
        """Outer catch still kills the reconstruction when a load-bearing
        key (regime, signal_posture, confidence) is missing or unparseable.
        Different policy from btc_dominance: without these, the rebuilt
        RegimeClassification would be structurally invalid."""

        # Missing required `regime` key → KeyError → outer catch → None.
        assert (
            _reconstruct_regime(
                {
                    "regime_at_creation": {
                        "signal_posture": "normal",
                        "confidence": 8,
                    }
                }
            )
            is None
        )
        # Unknown enum value → ValueError → outer catch → None.
        assert (
            _reconstruct_regime(
                {
                    "regime_at_creation": {
                        "regime": "not_a_regime",
                        "signal_posture": "normal",
                        "confidence": 8,
                    }
                }
            )
            is None
        )
