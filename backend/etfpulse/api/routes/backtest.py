"""Admin backtest routes (PR P2.4).

`POST /api/admin/backtest` runs the read-only backtest orchestrator
synchronously and returns a structured report. `GET /api/admin/backtest/detectors`
lists each detector's constructor kwargs so the FE form can render
the right inputs without duplicating the registry.

Discipline:
- Admin-keyed (require_admin_key dep). Operator surface, not public.
- Read-only contract — `await session.rollback()` after `run_backtest`
  as belt-and-braces (orchestrator does no writes by design; rollback
  is a no-op in practice but pins the invariant).
- `allow_ai=True` in the body requires an `X-Backtest-Allow-AI: yes`
  header to fire. Prevents an accidental misclick from burning the
  OpenRouter daily cap when the live-AI caller is wired in later.
  Today the route never passes a live caller through, so even with
  `allow_ai=True` + header, Tier 3 of make_resolver is a no-op — the
  gate is a forward-compatibility seam.
- Window size capped at `settings.backtest_max_window_days` (default 90)
  so long sweeps don't tie up a request worker beyond gateway timeout.
- Unknown detector names + unknown detector kwargs surface as 422 (not
  500). `run_backtest` raises `ValueError` for unknown detectors;
  `_build_detector` raises `TypeError` for unknown kwargs. The route
  catches both and translates.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from typing import cast, get_type_hints

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from etfpulse.api.deps import get_db_session, require_admin_key
from etfpulse.api.schemas.backtest import (
    BacktestDetectorOut,
    BacktestDetectorParamOut,
    BacktestDetectorsResponse,
    BacktestReportOut,
    BacktestRequest,
)
from etfpulse.config import settings
from etfpulse.pipeline.backtest import make_resolver, run_backtest
from etfpulse.pipeline.detectors import (
    AccelerationDetector,
    DivergenceDetector,
    FlowAnomalyDetector,
    MagnitudeDetector,
    RegimeShiftDetector,
)

log = structlog.get_logger()

router = APIRouter(prefix="/admin/backtest", tags=["admin"])


# Allowed scalar types for detector kwargs — mirrors
# `DetectorKwargValue` in schemas/backtest.py. Defined here in addition
# because the type-name introspection in `/detectors` needs to emit
# human-readable names matching the FE form's input widget vocabulary.
_TYPE_NAMES: dict[type, str] = {
    bool: "bool",
    int: "int",
    float: "float",
    Decimal: "Decimal",
    str: "str",
}


# Registry of detector classes the backtest harness knows about.
# Mirrors `_build_detector` in pipeline/backtest.py. Kept here as a
# tuple of (name, class, signal_type) so the listing route doesn't
# instantiate detectors (which would touch settings); it just
# introspects the class signature.
_BACKTEST_DETECTORS: list[tuple[str, type, str]] = [
    ("flow_anomaly", FlowAnomalyDetector, "flow_anomaly"),
    ("magnitude", MagnitudeDetector, "magnitude"),
    ("acceleration", AccelerationDetector, "acceleration"),
    ("divergence", DivergenceDetector, "divergence"),
    ("regime_shift", RegimeShiftDetector, "regime_shift"),
]


@router.post(
    "",
    response_model=BacktestReportOut,
    dependencies=[Depends(require_admin_key)],
    include_in_schema=False,
)
async def run_admin_backtest(
    body: BacktestRequest,
    session: AsyncSession = Depends(get_db_session),
    x_allow_ai: str | None = Header(default=None, alias="X-Backtest-Allow-AI"),
) -> dict[str, object]:
    """Execute a backtest sweep over `[body.start, body.end]` inclusive.

    Returns the structured report; the FE renders it as a per-detector
    comparison table.
    """
    if body.end < body.start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end date must be on or after start date",
        )

    window_days = (body.end - body.start).days + 1
    if window_days > settings.backtest_max_window_days:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"window {window_days}d exceeds cap "
                f"{settings.backtest_max_window_days}d; tighten the date range"
            ),
        )

    if body.allow_ai and x_allow_ai != "yes":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=("allow_ai=true requires header 'X-Backtest-Allow-AI: yes'"),
        )

    # Live-AI caller intentionally None — the seam is the body flag plus
    # the header; wiring the OpenRouter adapter as the actual caller is a
    # separate task. Today the worst case of a true-true config is a
    # tier-3-resolver-no-op, not an unbounded cost.
    resolver = make_resolver(
        session,
        allow_live_ai=body.allow_ai,
        live_ai_caller=None,
    )

    try:
        report = await run_backtest(
            session,
            start=body.start,
            end=body.end,
            detector_overrides=cast(
                "dict[str, dict[str, object]] | None",
                body.detector_overrides,
            ),
            ai_resolver=resolver,
        )
    except ValueError as e:
        # Raised by run_backtest on unknown detector names + invalid range.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        ) from e
    except TypeError as e:
        # Raised by _build_detector on unknown kwargs in detector_overrides.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid detector override kwargs: {e}",
        ) from e
    finally:
        # Read-only contract — rollback any implicit transaction state
        # the orchestrator's reads opened. No-op on the SQL side because
        # nothing was written; explicit for invariant clarity.
        await session.rollback()

    log.info(
        "backtest_route_completed",
        start=body.start.isoformat(),
        end=body.end.isoformat(),
        window_days=window_days,
        n_hits=sum(p.n_hits for p in report.per_detector),
        n_scored=sum(p.n_scored for p in report.per_detector),
    )
    return report.to_json_dict()


@router.get(
    "/detectors",
    response_model=BacktestDetectorsResponse,
    dependencies=[Depends(require_admin_key)],
    include_in_schema=False,
)
async def list_backtest_detectors() -> BacktestDetectorsResponse:
    """Return each detector's name + signal_type + constructor kwarg
    signatures. The FE form populates detector dropdown + threshold
    input pairs from this so the detector registry has ONE source of
    truth (the class signatures themselves)."""
    detectors = [
        _introspect_detector(name, cls, sig_type) for name, cls, sig_type in _BACKTEST_DETECTORS
    ]
    return BacktestDetectorsResponse(detectors=detectors)


def _introspect_detector(
    name: str,
    cls: type,
    signal_type: str,
) -> BacktestDetectorOut:
    """Build a typed param listing for one detector class.

    `inspect.signature(cls)` returns __init__'s signature with `self`
    already filtered. `get_type_hints` resolves any string-form
    annotations (PEP 563 / `from __future__ import annotations`) into
    actual type objects so `_annotation_name` can return a stable
    short label.
    """
    sig = inspect.signature(cls)
    hints = get_type_hints(cls.__init__)  # type: ignore[misc]
    params: list[BacktestDetectorParamOut] = []
    for param_name, param in sig.parameters.items():
        annotation = hints.get(param_name, param.annotation)
        has_default = param.default is not inspect.Parameter.empty
        default_value = _coerce_default(param.default) if has_default else None
        params.append(
            BacktestDetectorParamOut(
                name=param_name,
                type_name=_annotation_name(annotation),
                has_default=has_default,
                default=default_value,
            )
        )

    return BacktestDetectorOut(name=name, signal_type=signal_type, params=params)


def _annotation_name(annotation: object) -> str:
    """Translate a Python type annotation into a stable string the FE
    form uses to pick an input widget. All current detector kwargs are
    concrete scalar types — Union/Optional annotations would land in
    the fallback. The fallback returns `str` because every JSON value
    is string-representable; the FE renders unknown types as a text
    input."""
    if isinstance(annotation, type):
        return _TYPE_NAMES.get(annotation, annotation.__name__)
    return "str"


def _coerce_default(value: object) -> bool | int | float | str:
    """Coerce a detector kwarg default into a JSON-friendly scalar
    matching the `DetectorKwargValue` union. Decimal renders as its
    string form so the FE input renders a plain number string with no
    precision loss. All current detector defaults fall into these
    types; an unrecognised default at runtime raises so a future
    type addition surfaces in CI rather than silently emitting None."""
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported detector kwarg default type: {type(value).__name__}")
