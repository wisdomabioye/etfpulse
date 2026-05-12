"""Admin-only DTOs. Operator surface — not part of the public OpenAPI."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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

    # Branch 5 — Signals with status=ALERTED that have zero rows in
    # `signal_deliveries`. ALERTED is the post-fan-out terminal state for
    # the SIGNAL even when no recipients matched (edge case 14), so the
    # admin dashboard previously couldn't distinguish "delivered to 5
    # people" from "delivered to nobody because everyone's confidence
    # floor was higher than the signal's confidence". This counter
    # surfaces that mismatch. Steady-state value depends on operator
    # `pref_min_confidence` configs vs the actual confidence distribution
    # of recent signals — a persistent non-zero number isn't necessarily
    # a bug, but it's the signal to dig into with the delivery-trace
    # endpoint below.
    signals_alerted_with_zero_deliveries: int = Field(ge=0)

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


class EvalOutcomesResponse(BaseModel):
    """Result of a single `POST /api/admin/signals/eval-outcomes` invocation.

    Mirrors the per-tick summary `evaluate_pending_outcomes` returns. The
    operator-facing fields are `evaluated` (success count) and `remaining`
    (candidates left over after the limit truncation — > 0 means click
    again to drain). Skip/error counters help diagnose why a candidate
    didn't produce an outcome (NULL direction, unknown asset, no klines
    in window, etc).
    """

    candidates: int = Field(ge=0)
    evaluated: int = Field(ge=0)
    skipped_no_direction: int = Field(ge=0)
    skipped_unknown_asset: int = Field(ge=0)
    skipped_no_klines: int = Field(ge=0)
    skipped_no_bars_in_window: int = Field(ge=0)
    errored: int = Field(ge=0)
    remaining: int = Field(ge=0)


class DeliveryTraceRecipient(BaseModel):
    """One potential delivery target, with the verdict of each matching
    filter. Mirrors the rules in `pipeline.delivery._match_users` /
    `_match_groups` so an operator can read "user 7 didn't get this
    signal because their confidence floor is 6 but the signal was 4"
    directly off the response.

    `kind` distinguishes user vs group targets. For users, `chat_id` is
    the Telegram personal chat id from `notification_channels`; for
    groups, it's the `telegram_groups.chat_id`. `target_id` is the
    primary key of the user OR group row, namespace-distinct per `kind`.

    `exclude_reason` is populated only when `matched=False`. The first
    failing filter wins (most-specific cause); the renderer walks them
    in the same order `_match_users` evaluates.

    `delivery_status` is the status of the SignalDelivery row for this
    target, or None when no such row exists (matched=False AND no row,
    or matched=True but fan-out hasn't run yet).
    """

    kind: Literal["user", "group"]
    target_id: int
    target_label: str  # username for users, title for groups (best-effort)
    chat_id: str | int | None  # what we'd send to via Telegram

    # Per-filter verdicts (mirrors `_match_users` rule order)
    target_active: bool
    target_paused: bool
    channel_active: bool | None  # None for groups (no channel concept)
    asset_match: bool
    confidence_match: bool

    matched: bool
    exclude_reason: str | None = None

    # Delivery row state (when one exists)
    delivery_status: str | None = None
    delivery_attempts: int | None = None
    delivery_error: str | None = None


class DeliveryTrace(BaseModel):
    """Full diagnostic for one signal's delivery: who matched, who didn't,
    why, and what state each delivery row is in.

    The endpoint that returns this (`GET /api/admin/signals/{id}/delivery-trace`)
    solves the "did this signal reach anyone, and if not, why?" question
    without dropping to SQL. Use cases:
        - Admin Run Cycle produced a signal but you didn't get an alert
          on Telegram → check this endpoint, see exclude_reason for your
          user (likely "confidence below user's min" or "channel inactive").
        - A signal is stuck in PENDING for days → see the delivery row
          attempts + error.

    `delivery_count` is the total number of `signal_deliveries` rows
    pointing to this signal. `delivered_count` etc. break it down by
    status. `matched_count` is how many recipients SHOULD have received
    the signal per the filter rules — equal to delivery_count when
    fan-out has run on this signal.
    """

    signal_id: int
    signal_asset: str
    signal_type: str
    signal_confidence: int | None
    signal_status: str

    delivery_count: int = Field(ge=0)
    delivered_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)

    matched_count: int = Field(ge=0)
    recipients: list[DeliveryTraceRecipient]


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
