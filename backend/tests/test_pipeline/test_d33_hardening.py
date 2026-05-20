"""Regression tests for PR D.3.3 fixes.

Pins:
  - Scheduler closes SoDEX HTTP clients on POST-enter failure
    (e.g., `scheduler.start()` raising after both clients entered).
    The D.3.2 narrow try/except didn't cover this; D.3.3 widened to
    `async with AsyncExitStack()`.
  - symbols_refresh continues across a DB-level row failure thanks to
    SAVEPOINT (begin_nested) per row. Without it, one failed
    `session.execute` poisons the session and every subsequent row
    raises InvalidRequestError.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from etfpulse.adapters.sodex.perps_client import SodexPerpsClient
from etfpulse.adapters.sodex.responses import SpotSymbol
from etfpulse.adapters.sodex.spot_client import SodexSpotClient
from etfpulse.config import settings
from etfpulse.models import SodexSymbol
from etfpulse.pipeline.scheduler import start_scheduler
from etfpulse.pipeline.symbols_refresh import refresh_sodex_symbols

# ---------------------------------------------------------------------------
# D.3.3 step 1 — scheduler closes clients on post-enter exception
# ---------------------------------------------------------------------------


class TestSchedulerClientCleanupOnSetupFailure:
    async def test_clients_close_when_scheduler_start_fails(self, monkeypatch):
        """Simulate scheduler.start() raising AFTER both SoDEX clients
        have entered. D.3.2's narrow try/except (around just the two
        enters) would NOT close the clients here; D.3.3's
        `async with AsyncExitStack()` does. We assert by patching the
        SodexHttpClient.close() and verifying it gets called even
        though scheduler.start() blew up before yield.
        """
        monkeypatch.setattr(settings, "run_scheduler", True)

        # Patch the catch-up check so the test doesn't hit the DB
        # before scheduler.start().
        async def _stub_no_catchup(session):
            return False

        monkeypatch.setattr("etfpulse.pipeline.scheduler._needs_catchup", _stub_no_catchup)

        # Patch SodexHttpClient.close so we can assert it ran.
        close_calls: list[None] = []

        async def _tracking_close(self):
            close_calls.append(None)

        with patch(
            "etfpulse.adapters.sodex._http.SodexHttpClient.close",
            new=_tracking_close,
        ):
            # Patch AsyncIOScheduler.start to RAISE after the clients
            # have entered.
            with patch(
                "etfpulse.pipeline.scheduler.AsyncIOScheduler.start",
                side_effect=RuntimeError("simulated scheduler start failure"),
            ):
                app = FastAPI()
                with pytest.raises(RuntimeError, match="simulated scheduler"):
                    async with start_scheduler(app):
                        pass  # never reached — start() raises pre-yield

        # CRITICAL: both clients (spot + perps) must have been closed
        # even though setup never reached the yield. Each client's
        # close() runs once → at least 2 calls. (Idempotent close()
        # may run more if the test patching introduces extra paths;
        # the invariant is "at least both, never zero.")
        assert len(close_calls) >= 2, (
            f"Expected at least 2 SodexHttpClient.close() calls (spot + "
            f"perps), got {len(close_calls)}. The clients leaked."
        )


# ---------------------------------------------------------------------------
# D.3.3 step 2 — SAVEPOINT per row in symbols_refresh
# ---------------------------------------------------------------------------


class TestSymbolsRefreshSavepointPerRow:
    async def test_db_level_row_failure_does_not_poison_session(self, db_session):
        """Force a DB-level failure mid-batch via a mocked `_upsert_symbol`.
        WITHOUT the SAVEPOINT (D.3.3 fix), the session would enter
        failed-tx state on the first error and every subsequent
        `session.execute` would raise InvalidRequestError. WITH the
        SAVEPOINT, the session stays usable + subsequent rows persist.

        We mock `_upsert_symbol` to raise on a specific symbol id,
        succeed on others. Asserts: (a) good rows persisted, (b) bad
        row counted in parse_errors, (c) session is still healthy
        after the test.
        """
        spot_symbols = [
            SpotSymbol.model_construct(id=1, name="vBTC_vUSDC"),
            SpotSymbol.model_construct(id=2, name="vETH_vUSDC"),  # will fail
            SpotSymbol.model_construct(id=3, name="vSOL_vUSDC"),
        ]
        spot = AsyncMock(spec=SodexSpotClient)
        spot.get_symbols = AsyncMock(return_value=spot_symbols)
        perps = AsyncMock(spec=SodexPerpsClient)
        perps.get_symbols = AsyncMock(return_value=[])

        # Real upsert by default; raise on id=2.
        from etfpulse.pipeline import symbols_refresh as sr_mod

        original_upsert = sr_mod._upsert_symbol

        async def _failing_upsert(session, *, venue, symbol_id, name, asset, raw):
            if symbol_id == 2:
                # Simulate a real DB-level failure that would normally
                # poison the session (e.g., asyncpg connection issue).
                # Raising AFTER touching the session through a real
                # execute is what triggers failed-tx state.
                from sqlalchemy import text as sa_text

                await session.execute(sa_text("SELECT 1/0"))  # division-by-zero
                # Unreachable
                return await original_upsert(
                    session,
                    venue=venue,
                    symbol_id=symbol_id,
                    name=name,
                    asset=asset,
                    raw=raw,
                )
            return await original_upsert(
                session,
                venue=venue,
                symbol_id=symbol_id,
                name=name,
                asset=asset,
                raw=raw,
            )

        with patch.object(sr_mod, "_upsert_symbol", new=_failing_upsert):
            summary = await refresh_sodex_symbols(db_session, spot_client=spot, perps_client=perps)

        # Good rows landed; bad row counted.
        assert summary["spot_inserted"] == 2
        assert summary["spot_parse_errors"] == 1

        # Verify the session is still healthy: we can read back the
        # symbols we inserted (failed-tx state would block this).
        rows = (
            (await db_session.execute(select(SodexSymbol).order_by(SodexSymbol.symbol_id)))
            .scalars()
            .all()
        )
        assert {r.symbol_id for r in rows} == {1, 3}


# ---------------------------------------------------------------------------
# D.3.2/D.3.3 — admin halt 409 on legitimate race
# (the existing fix had no test; covering here)
# ---------------------------------------------------------------------------


class TestAdminHalt409OnRace:
    async def test_returns_409_when_record_none_and_find_active_none(self, monkeypatch):
        """If `record()` returns None (conflict — already-active) but
        `find_active()` then returns None (a parallel resume just
        cleared it), the route MUST return 409 Conflict — distinguishing
        a legitimate concurrent admin action from a 503 system failure.
        """
        import httpx
        from httpx import ASGITransport

        from etfpulse.api.deps import get_db_session
        from etfpulse.app import create_app

        monkeypatch.setattr(settings, "admin_api_key", "secret-key")

        # Patch circuit_breaker to simulate the race:
        #   record() returns None (conflict path)
        #   find_active() returns None (cleared by concurrent resume)
        async def _stub_record(session, trigger_type, details=None, user_id=None):
            return None

        async def _stub_find_active(session, trigger_type, user_id=None):
            return None

        monkeypatch.setattr("etfpulse.api.routes.admin.circuit_breaker.record", _stub_record)
        monkeypatch.setattr(
            "etfpulse.api.routes.admin.circuit_breaker.find_active",
            _stub_find_active,
        )

        app = create_app()

        # We don't need a real db_session for this race-path test, but the
        # dependency still wires up; override with a MagicMock that
        # supports the operations the route uses.
        async def _stub_session():
            session = MagicMock()
            session.commit = AsyncMock()
            yield session

        app.dependency_overrides[get_db_session] = _stub_session
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/admin/execution/halt",
                headers={"X-Admin-Key": "secret-key"},
                json={"reason": "race-test"},
            )

        app.dependency_overrides.clear()
        assert resp.status_code == 409
        assert "concurrent admin action" in resp.json()["detail"]
