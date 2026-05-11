"""Admin-only DTOs. Operator surface — not part of the public OpenAPI."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SignalStatusCounts(BaseModel):
    """Live counts of `signals.status` across all rows."""

    pending: int = Field(ge=0)
    alerted: int = Field(ge=0)
    expired: int = Field(ge=0)


class DeliveryStatusCounts(BaseModel):
    """Live counts of `signal_deliveries.status` across all rows."""

    pending: int = Field(ge=0)
    delivered: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)


class SchedulerJobInfo(BaseModel):
    """One row per APScheduler job. Lets the operator distinguish
    "scheduler is healthy, just hasn't ticked yet" from "scheduler is dead."

    `next_run_at` is None when the job is paused or its next fire time
    isn't yet computed. `pending` is True when the job is waiting on its
    first dispatch — non-pending + None next_run means the scheduler is
    paused or stopped.
    """

    id: str
    next_run_at: datetime | None
    trigger: str
    pending: bool


class AdminMetrics(BaseModel):
    """Operator dashboard payload — exposes reaper effects + queue depth.

    All counts are point-in-time snapshots from a single DB read. No
    historical series — the use case is "is something stuck right now?"
    not "graph the trend." A future Prometheus exporter could scrape this
    same shape on an interval.
    """

    signal_status_counts: SignalStatusCounts
    delivery_status_counts: DeliveryStatusCounts

    # Signals whose `expires_at` has passed but status is still PENDING/ALERTED.
    # Steady-state should be ≈0 — the expiry reaper runs every 15 min, so a
    # non-zero number means either the reaper hasn't run yet, the scheduler
    # is halted, or these rows have a > 15 min lag window. Persistent
    # non-zero = scheduler problem.
    signals_overdue_unreaped: int = Field(ge=0)

    # Signals with NULL confidence (AI failed at build time, per known
    # behavior). These accumulate forever in PENDING because they have no
    # expires_at to compare. Surfaced here so operators can spot runaway
    # OpenRouter failures early.
    signals_null_confidence: int = Field(ge=0)

    # SignalDelivery rows in PENDING longer than
    # `delivery_pending_max_age_seconds`. The delivery reaper will flip
    # these to FAILED on its next tick — non-zero here means either the
    # reaper hasn't run yet OR the send worker stopped picking up rows.
    deliveries_stuck_pending: int = Field(ge=0)

    # SignalDelivery rows whose error_message matches the reaper sentinel.
    # Counts the all-time total — a rising number means the send worker is
    # missing deadlines (bot disabled mid-flight, scheduler halted, etc).
    deliveries_reaper_failures: int = Field(ge=0)

    # APScheduler job introspection — None when `run_scheduler=false` (no
    # scheduler attached to app.state). Empty list is theoretically possible
    # but in practice the scheduler always has the daily-cycle + reaper
    # jobs registered. Use the `next_run_at` per row to spot a scheduler
    # that's running but has stopped advancing its timers.
    scheduler_jobs: list[SchedulerJobInfo] | None = None

    # Size of `app.state.telegram_webhook_secrets` — surfaces stuck
    # rotations (issue #40). Steady state is 1; a value of 2+ means a
    # widen-then-shrink rotation didn't complete (process killed
    # mid-rotation, or the operator pinned the set artificially).
    # None when the bot is disabled (no state to inspect).
    accepted_webhook_secrets: int | None = None

    # Issue #32 — currently active prompt version (from
    # `pipeline.analysis.AI_PROMPT_VERSION`). The version of every NEW
    # signal stamped from this process. Operator compares against
    # `signal_counts_by_prompt_version` below to see how the cohort
    # composition is shifting after a bump.
    current_ai_prompt_version: str

    # Distribution of `signals.ai_prompt_version` across the full table.
    # Useful for spotting "we have 2000 v2 signals and 30 v3 signals" —
    # the track-record headline is meaningless until the v3 cohort gets
    # a meaningful sample. Dict ordering is by descending count for
    # convenient inspection in dashboards.
    signal_counts_by_prompt_version: dict[str, int]


class RetryAiErrorSample(BaseModel):
    """Per-row failure detail emitted by `POST /api/admin/signals/retry-ai`.

    Capped to 3 samples per response so a backlog where every row fails
    for the same reason (e.g. account out of credits → all hit 402)
    doesn't bloat the payload. The kind / detail pair is enough for an
    operator to decide whether the next click will help or whether the
    underlying issue (credits, model slug, schema) needs fixing first.
    """

    signal_id: int
    kind: str
    detail: str


class RetryAiResponse(BaseModel):
    """Result of a single `POST /api/admin/signals/retry-ai` invocation.

    `scanned` is bounded by the request `limit` (caps OpenRouter spend per
    click). `updated + failed == scanned` in every well-formed response.
    Operators re-fire until `scanned == 0` to drain the backlog.
    """

    scanned: int = Field(ge=0)
    updated: int = Field(ge=0)
    failed: int = Field(ge=0)
    error_samples: list[RetryAiErrorSample] = Field(default_factory=list)
