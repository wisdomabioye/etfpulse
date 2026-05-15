"""fan_out_signal — matching logic + idempotency + edge cases.

Tests seed Signal + User(+Channel) + TelegramGroup rows in db_session,
call fan_out_signal(db_session, signal_id), and assert on the resulting
SignalDelivery rows + signal.status transitions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from etfpulse.models import (
    ChannelType,
    NotificationChannel,
    Signal,
    SignalDelivery,
    SignalStatus,
    TelegramGroup,
    User,
)
from etfpulse.pipeline.delivery import fan_out_signal
from etfpulse.pipeline.detectors import compute_fingerprint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_signal(
    db_session,
    *,
    asset: str = "BTC",
    confidence: int | None = 7,
    status: str = SignalStatus.PENDING.value,
    expires_at: datetime | None = None,
) -> Signal:
    signal = Signal(
        signal_type="flow_anomaly",
        asset=asset,
        trigger_data={},
        ai_analysis={"headline": "test"} if confidence is not None else None,
        confidence=confidence,
        status=status,
        expires_at=expires_at,
        fingerprint=compute_fingerprint(asset, "fan-test", str(confidence or "none")),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    await db_session.flush()
    return signal


async def _make_user(
    db_session,
    *,
    chat_id: int,
    pref_assets: list[str] | None = None,
    pref_min_confidence: int = 5,
    pref_paused: bool = False,
    is_active: bool = True,
    channel_active: bool = True,
) -> tuple[User, NotificationChannel]:
    user = User(
        is_active=is_active,
        pref_assets=pref_assets if pref_assets is not None else ["BTC", "ETH"],
        pref_min_confidence=pref_min_confidence,
        pref_paused=pref_paused,
    )
    db_session.add(user)
    await db_session.flush()

    channel = NotificationChannel(
        user_id=user.id,
        channel_type=ChannelType.TELEGRAM.value,
        channel_identifier=str(chat_id),
        is_active=channel_active,
    )
    db_session.add(channel)
    await db_session.flush()
    return user, channel


async def _make_group(
    db_session,
    *,
    chat_id: int,
    pref_assets: list[str] | None = None,
    pref_min_confidence: int = 5,
    is_active: bool = True,
    pref_paused: bool = False,
) -> TelegramGroup:
    group = TelegramGroup(
        chat_id=chat_id,
        is_active=is_active,
        pref_assets=pref_assets if pref_assets is not None else ["BTC", "ETH"],
        pref_min_confidence=pref_min_confidence,
        pref_paused=pref_paused,
    )
    db_session.add(group)
    await db_session.flush()
    return group


async def _deliveries_for(db_session, signal_id: int) -> list[SignalDelivery]:
    result = await db_session.execute(
        select(SignalDelivery).where(SignalDelivery.signal_id == signal_id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Happy-path matching
# ---------------------------------------------------------------------------


class TestFanOutMatching:
    async def test_matches_single_user(self, db_session):
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        user, channel = await _make_user(
            db_session, chat_id=100, pref_assets=["BTC"], pref_min_confidence=5
        )

        count = await fan_out_signal(db_session, signal.id)

        assert count == 1
        deliveries = await _deliveries_for(db_session, signal.id)
        assert len(deliveries) == 1
        assert deliveries[0].user_id == user.id
        assert deliveries[0].channel_id == channel.id
        assert deliveries[0].group_id is None

    async def test_matches_multiple_users_and_groups(self, db_session):
        signal = await _make_signal(db_session, asset="BTC", confidence=7)

        # Three matching users
        await _make_user(db_session, chat_id=101, pref_min_confidence=5)
        await _make_user(db_session, chat_id=102, pref_min_confidence=6)
        await _make_user(db_session, chat_id=103, pref_min_confidence=7)
        # One matching group
        await _make_group(db_session, chat_id=-100900, pref_min_confidence=4)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 4  # 3 users + 1 group

    async def test_empty_pref_assets_means_all(self, db_session):
        """Edge case 15 — user with pref_assets=[] accepts every asset."""
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        user, _ = await _make_user(db_session, chat_id=200, pref_assets=[], pref_min_confidence=5)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 1
        deliveries = await _deliveries_for(db_session, signal.id)
        assert deliveries[0].user_id == user.id


# ---------------------------------------------------------------------------
# Filtering — who gets excluded
# ---------------------------------------------------------------------------


class TestFanOutFilters:
    async def test_respects_min_confidence(self, db_session):
        """User with pref_min_confidence=8 must NOT match a confidence=7 signal."""
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_user(db_session, chat_id=300, pref_min_confidence=8)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0

    async def test_asset_mismatch_excludes_user(self, db_session):
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_user(db_session, chat_id=301, pref_assets=["ETH"])

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0

    async def test_inactive_user_excluded(self, db_session):
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_user(db_session, chat_id=302, is_active=False)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0

    async def test_paused_user_excluded(self, db_session):
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_user(db_session, chat_id=303, pref_paused=True)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0

    async def test_inactive_channel_excluded(self, db_session):
        """User active but their Telegram channel is inactive (e.g. they
        blocked the bot and it got marked inactive) → no delivery."""
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_user(db_session, chat_id=304, channel_active=False)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0

    async def test_inactive_group_excluded(self, db_session):
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_group(db_session, chat_id=-100950, is_active=False)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0

    async def test_paused_group_excluded(self, db_session):
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_group(db_session, chat_id=-100960, pref_paused=True)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0


# ---------------------------------------------------------------------------
# Signal state / skip conditions
# ---------------------------------------------------------------------------


class TestFanOutSkipConditions:
    async def test_missing_signal_returns_zero(self, db_session):
        count = await fan_out_signal(db_session, signal_id=999_999_999)
        assert count == 0

    async def test_expired_signal_skipped(self, db_session):
        """Edge case 16 — expired signal doesn't fan out, doesn't change status."""
        past = datetime.now(UTC) - timedelta(hours=1)
        signal = await _make_signal(db_session, asset="BTC", confidence=7, expires_at=past)
        await _make_user(db_session, chat_id=400, pref_min_confidence=5)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0
        await db_session.refresh(signal)
        # Status UNCHANGED — the reaper job (issue #30) will flip to EXPIRED.
        assert signal.status == SignalStatus.PENDING.value

    async def test_already_alerted_signal_skipped(self, db_session):
        """Idempotent re-call — already-ALERTED signal returns 0."""
        signal = await _make_signal(
            db_session,
            asset="BTC",
            confidence=7,
            status=SignalStatus.ALERTED.value,
        )
        await _make_user(db_session, chat_id=401, pref_min_confidence=5)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0
        # No deliveries created.
        assert await _deliveries_for(db_session, signal.id) == []

    async def test_null_confidence_signal_skipped(self, db_session):
        """AI failure → signal stays PENDING, fan-out skips (doesn't send
        low-quality alerts to everyone). Flagged decision — see delivery.py."""
        signal = await _make_signal(db_session, asset="BTC", confidence=None)
        await _make_user(db_session, chat_id=402, pref_min_confidence=5)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0
        await db_session.refresh(signal)
        assert signal.status == SignalStatus.PENDING.value


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


class TestFanOutStatus:
    async def test_marks_alerted_after_successful_fan_out(self, db_session):
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_user(db_session, chat_id=500, pref_min_confidence=5)

        await fan_out_signal(db_session, signal.id)
        await db_session.refresh(signal)
        assert signal.status == SignalStatus.ALERTED.value

    async def test_marks_alerted_even_with_zero_recipients(self, db_session):
        """Edge case 14 — the fan-out work is "done" whether or not anyone
        was subscribed. Otherwise orphan PENDING signals pile up when no
        users/groups match the asset/confidence."""
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        # No users, no groups created.

        count = await fan_out_signal(db_session, signal.id)
        assert count == 0
        await db_session.refresh(signal)
        assert signal.status == SignalStatus.ALERTED.value

    async def test_emits_no_recipients_event_when_zero(self, db_session, capsys):
        """Branch 5 — distinct log event for the "fanned out to nobody"
        case. Greppable separately from `fan_out_signal_done` so operators
        can alert on it without scanning every fan_out_signal_done line
        for `inserted=0`.

        Uses `capsys` instead of `structlog.testing.capture_logs()` because
        the project calls `structlog.configure(cache_logger_on_first_use=True)`
        in `api.logging_config`. Once another test boots the FastAPI app,
        bound loggers are cached and `capture_logs()` no longer intercepts
        them. Reading stdout is robust against config-caching across the
        full test suite.
        """
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        # No users, no groups → zero recipients.

        await fan_out_signal(db_session, signal.id)

        captured = capsys.readouterr().out
        assert "fan_out_signal_no_recipients" in captured

    async def test_no_recipients_event_skipped_when_inserted(self, db_session, capsys):
        """Symmetric guard — when fan-out DID insert rows, the
        no-recipients event must NOT fire. Otherwise the metric/alert
        becomes useless noise."""
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_user(db_session, chat_id=600, pref_min_confidence=5)

        count = await fan_out_signal(db_session, signal.id)
        assert count == 1

        captured = capsys.readouterr().out
        assert "fan_out_signal_no_recipients" not in captured


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestFanOutIdempotency:
    async def test_double_fan_out_no_duplicates(self, db_session):
        """Calling fan_out_signal twice on the same signal must not create
        duplicate SignalDelivery rows. Relies on the partial UNIQUE indexes
        from #57 + ON CONFLICT DO NOTHING. In practice the second call
        also early-returns because status flipped to ALERTED, but this test
        reaches deeper — flip status back to PENDING to force the insert."""
        signal = await _make_signal(db_session, asset="BTC", confidence=7)
        await _make_user(db_session, chat_id=600, pref_min_confidence=5)

        first = await fan_out_signal(db_session, signal.id)
        assert first == 1

        # Reset status to re-exercise the INSERT path.
        signal.status = SignalStatus.PENDING.value
        await db_session.flush()

        second = await fan_out_signal(db_session, signal.id)
        # ON CONFLICT DO NOTHING → 0 new rows.
        assert second == 0
        assert len(await _deliveries_for(db_session, signal.id)) == 1


# ---------------------------------------------------------------------------
# Smoke — Decimal flow data doesn't crash
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MARKET sentinel (PR F.3) — regime_shift signals bypass `pref_assets`.
# ---------------------------------------------------------------------------


class TestFanOutMarketSentinel:
    """A MARKET signal must reach every active+non-paused recipient that
    meets the confidence floor, regardless of their `pref_assets` setting
    (which lists user-selectable assets like BTC/ETH and would otherwise
    exclude MARKET as 'not in my asset list').
    """

    async def test_market_signal_delivers_to_user_with_btc_only_prefs(self, db_session):
        """User opted into BTC only — but a market-wide regime shift still
        reaches them. That's the whole point of the MARKET sentinel; if it
        respected pref_assets, no one would ever receive a regime_shift
        since 'MARKET' isn't in anyone's pref_assets array."""
        signal = await _make_signal(db_session, asset="MARKET", confidence=7)
        user, channel = await _make_user(
            db_session, chat_id=800, pref_assets=["BTC"], pref_min_confidence=5
        )

        count = await fan_out_signal(db_session, signal.id)

        assert count == 1
        deliveries = await _deliveries_for(db_session, signal.id)
        assert len(deliveries) == 1
        assert deliveries[0].user_id == user.id
        assert deliveries[0].channel_id == channel.id

    async def test_market_signal_delivers_to_user_with_empty_prefs(self, db_session):
        """`pref_assets=[]` (the "all assets" sentinel) — still receives
        MARKET, no regression from the existing all-assets path."""
        signal = await _make_signal(db_session, asset="MARKET", confidence=7)
        await _make_user(db_session, chat_id=801, pref_assets=[], pref_min_confidence=5)

        count = await fan_out_signal(db_session, signal.id)

        assert count == 1

    async def test_market_signal_delivers_to_group_with_eth_only_prefs(self, db_session):
        """Group analogue — `_match_groups` must mirror `_match_users` for
        MARKET signals. Pre-PR-F.3 a group with `pref_assets=["ETH"]` would
        never receive a regime_shift."""
        signal = await _make_signal(db_session, asset="MARKET", confidence=7)
        await _make_group(db_session, chat_id=-100800, pref_assets=["ETH"], pref_min_confidence=5)

        count = await fan_out_signal(db_session, signal.id)

        assert count == 1

    async def test_market_signal_excluded_by_paused_user(self, db_session):
        """MARKET bypasses `pref_assets`, NOT `pref_paused`. Paused users
        still don't receive anything — that's the universal opt-out lever."""
        signal = await _make_signal(db_session, asset="MARKET", confidence=7)
        await _make_user(db_session, chat_id=802, pref_paused=True, pref_min_confidence=5)

        count = await fan_out_signal(db_session, signal.id)

        assert count == 0

    async def test_market_signal_excluded_by_low_confidence(self, db_session):
        """MARKET bypasses `pref_assets`, NOT `pref_min_confidence`. A user
        with a high floor still filters MARKET signals below it."""
        signal = await _make_signal(db_session, asset="MARKET", confidence=4)
        await _make_user(db_session, chat_id=803, pref_min_confidence=7)

        count = await fan_out_signal(db_session, signal.id)

        assert count == 0

    async def test_market_signal_excluded_by_inactive_channel(self, db_session):
        """Active user but inactive Telegram channel (e.g. blocked) — no
        delivery, same as non-MARKET signals."""
        signal = await _make_signal(db_session, asset="MARKET", confidence=7)
        await _make_user(db_session, chat_id=804, channel_active=False)

        count = await fan_out_signal(db_session, signal.id)

        assert count == 0

    async def test_market_signal_delivers_to_mix_of_recipients(self, db_session):
        """End-to-end — MARKET fans out to several users with diverse prefs
        + a group simultaneously. Pins the cross-asset reach property."""
        signal = await _make_signal(db_session, asset="MARKET", confidence=7)
        await _make_user(
            db_session, chat_id=805, pref_assets=["BTC"], pref_min_confidence=5
        )  # match
        await _make_user(
            db_session, chat_id=806, pref_assets=["ETH"], pref_min_confidence=5
        )  # match
        await _make_user(db_session, chat_id=807, pref_assets=[], pref_min_confidence=5)  # match
        await _make_user(
            db_session, chat_id=808, pref_min_confidence=9, pref_assets=["BTC"]
        )  # excluded by confidence
        await _make_group(
            db_session, chat_id=-100801, pref_assets=["ETH"], pref_min_confidence=5
        )  # match

        count = await fan_out_signal(db_session, signal.id)

        assert count == 4  # 3 matching users + 1 matching group


# ---------------------------------------------------------------------------
# Smoke — Decimal flow data doesn't crash
# ---------------------------------------------------------------------------


async def test_fan_out_works_with_decimal_price(db_session):
    """Sanity — signal with Decimal price_at_creation set doesn't break the
    match query (#34 says NULL is expected for now, but test both paths)."""
    signal = Signal(
        signal_type="flow_anomaly",
        asset="BTC",
        trigger_data={},
        confidence=7,
        price_at_creation=Decimal("42000.00"),
        fingerprint=compute_fingerprint("decimal-price-test"),
        signal_date=date(2026, 4, 23),
    )
    db_session.add(signal)
    await db_session.flush()

    await _make_user(db_session, chat_id=700, pref_min_confidence=5)
    count = await fan_out_signal(db_session, signal.id)
    assert count == 1
