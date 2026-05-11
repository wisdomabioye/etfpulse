/**
 * TanStack Query hooks — one per backend endpoint.
 *
 * Pages consume `useDashboardStats()`, `useSignals()`, `useInfiniteSignals()`,
 * `useSignal(id)`. Loading + error + caching + dedup are TanStack defaults
 * (configured in main.tsx: 30s staleTime, retry once, no refetch-on-focus).
 *
 * Query-key convention: `['<resource>', ...specifiers]`. Stable across
 * re-renders — TanStack deep-compares so identical filter VALUES (even on
 * fresh object references) hit the cache.
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, apiGet, apiPost } from './client';
import type {
  AdminMetrics,
  DashboardStats,
  PaginatedSignals,
  PaginatedTrackRecord,
  RegimeResponse,
  SignalDetail,
  SignalFilters,
  TrackRecordBreakdown,
  TrackRecordFilters,
} from './types';

/** Readiness body returned by `GET /api/health/ready`. Shape is stable across
 *  200 (ok) and 503 (degraded) — see backend `health.py:readiness`. We read
 *  the body in both cases so the UI can distinguish "DB down" from "warnings
 *  only" rather than collapsing both into a single error state. */
export interface ReadinessResponse {
  status: 'ok' | 'degraded';
  db: 'ok' | 'error';
  config: { errors: string[]; warnings: string[] };
}

/** Three-state liveness derived from readiness:
 *  - `live` — 200, no config errors/warnings
 *  - `degraded` — 200 with warnings, OR 503 with warnings only (DB fine)
 *  - `down` — 503 with `db: error`, or network failure */
export type LivenessState = 'live' | 'degraded' | 'down';

export interface Liveness {
  state: LivenessState;
  readiness: ReadinessResponse | null;
}

const API_BASE = import.meta.env.VITE_API_BASE_URL || '';

/** Custom fetch — readiness returns 503 with a JSON body on degraded, which
 *  the standard `apiGet` would throw on. We read the body in both cases. */
async function fetchReadiness(): Promise<Liveness> {
  try {
    const res = await fetch(`${API_BASE}/api/health/ready`, {
      headers: { Accept: 'application/json' },
    });
    const body = (await res.json()) as ReadinessResponse;
    if (res.ok && body.status === 'ok') {
      return { state: 'live', readiness: body };
    }
    if (body.db === 'error') {
      return { state: 'down', readiness: body };
    }
    return { state: 'degraded', readiness: body };
  } catch {
    return { state: 'down', readiness: null };
  }
}

/** Liveness pulse — polls /api/health/ready every 30s. Tolerant of 503
 *  (degraded body is informative, not an error). Never retries — the next
 *  poll tick is the retry. */
export function useLiveness() {
  return useQuery({
    queryKey: ['liveness'],
    queryFn: fetchReadiness,
    refetchInterval: 30_000,
    retry: false,
    staleTime: 0,
  });
}

/** Home page headline tiles. Refetches every 30s (TanStack staleTime default). */
export function useDashboardStats() {
  return useQuery({
    queryKey: ['dashboard', 'stats'],
    queryFn: () => apiGet<DashboardStats>('/api/dashboard/stats'),
  });
}

/** Single page of signals — for the home page "last 3" and similar bounded
 * lists. Use `useInfiniteSignals` for the feed page. */
export function useSignals(filters?: SignalFilters) {
  return useQuery({
    queryKey: ['signals', filters ?? {}],
    queryFn: () => apiGet<PaginatedSignals>('/api/signals', filters),
  });
}

/** Cursor-paginated infinite list — backs the /signals feed page.
 * `data.pages` is a list of PaginatedSignals; flatten with `pages.flatMap(p => p.items)`.
 * Call `fetchNextPage()` from a "Load more" button or IntersectionObserver. */
export function useInfiniteSignals(filters?: SignalFilters) {
  return useInfiniteQuery({
    queryKey: ['signals', 'infinite', filters ?? {}],
    queryFn: ({ pageParam }) =>
      apiGet<PaginatedSignals>('/api/signals', {
        ...filters,
        cursor: pageParam,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
}

/** Latest market-regime classification (Stage 7-P7).
 *
 * 503 from the endpoint = "no snapshot yet" (cold-boot OR legacy pre-Stage-7
 * row). That is NOT a transient error — retrying won't make a snapshot
 * appear, so we short-circuit. Any other error (network blip, 502 from the
 * proxy) gets the TanStack default of one retry. The page also exposes a
 * manual retry button via `query.refetch()`.
 *
 * Same 30s staleTime default as `useDashboardStats` — they refetch on the
 * same cadence so the home page's regime tile and the dedicated /regime
 * page stay coherent within a tab session. */
export function useRegime() {
  return useQuery({
    queryKey: ['regime'],
    queryFn: () => apiGet<RegimeResponse>('/api/regime'),
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 503) return false;
      return failureCount < 1;
    },
  });
}

/** Diagnostic breakdown for `/analytics` (Stage 8-P10).
 *
 *  Backend caches the result for 5 min in-process (`pipeline.analytics`), so
 *  there's no benefit to a long FE staleTime — we let TanStack's default
 *  (30s) handle re-render hops within a session, but the actual DB query
 *  fires at most once per 5-min window per backend worker regardless.
 *
 *  Retry off — cold-boot returns 200 with empty arrays (not 503), so any
 *  error here is a config / network issue that won't fix itself on retry. */
export function useAnalyticsBreakdown() {
  return useQuery({
    queryKey: ['analytics', 'breakdown'],
    queryFn: () => apiGet<TrackRecordBreakdown>('/api/analytics/breakdown'),
    retry: false,
  });
}

/** Paginated track-record list + same-filter summary stats (Stage 8-P4).
 *
 * Mirrors `useSignals` shape — single page per call, filters live in the
 * queryKey so changing them invalidates the cache cleanly. The track-record
 * page uses page-mode pagination (numbered pager); cursor mode is also
 * supported by the endpoint but not used by any caller today.
 *
 * Empty-DB / cold-boot returns `summary` with all zeros + null hit_rate
 * — the page handles that as the empty state, not an error. */
export function useTrackRecord(filters?: TrackRecordFilters) {
  return useQuery({
    queryKey: ['track-record', filters ?? {}],
    queryFn: () => apiGet<PaginatedTrackRecord>('/api/track-record', filters),
  });
}

/** Admin metrics — gated by `X-Admin-Key`. The hook is disabled while
 * `adminKey` is empty so we don't fire requests with no auth (would 401
 * and add noise to logs). Refetches every 15s — operator dashboards want
 * fresh state, not 30s-stale snapshots. Retries are off because a 401 or
 * 503 is a config issue, not transient.
 */
export function useAdminMetrics(adminKey: string) {
  return useQuery({
    queryKey: ['admin', 'metrics', adminKey],
    queryFn: () =>
      apiGet<AdminMetrics>('/api/admin/metrics', undefined, {
        'X-Admin-Key': adminKey,
      }),
    enabled: adminKey.length > 0,
    refetchInterval: 15_000,
    retry: false,
  });
}

/** Shape of `POST /api/admin/signals/trigger` response — same as the cycle
 *  summary the scheduler logs. Kept inline here (not in `types.ts`) because
 *  no other surface consumes it; promote when a second caller appears. */
export interface TriggerCycleResponse {
  ingested: Record<string, number>;
  ingest_errors: [string, string][];
  news_ingested: Record<string, number>;
  news_errors: [string, string][];
  prices: Record<string, { source: string; price: string }>;
  price_errors: string[];
  regime: {
    regime: string;
    signal_posture: string;
    confidence: number;
    macro_events_nearby: string[];
  } | null;
  regime_error: string | null;
  detectors_run: number;
  detector_errors: [string, string][];
  signals_new: number;
  signals_duplicate: number;
  ai_succeeded: number;
  ai_failed: number;
}

/** Fire one synchronous run of the daily signal cycle.
 *
 * Mutation, not query: the operator decides when to fire (button click).
 * On success we invalidate the metrics query so the dashboard reflects
 * the new state immediately rather than waiting up to 15s for the next
 * auto-refresh tick. Retry off — a 4xx/5xx is a config / pipeline issue,
 * not a transient blip, and re-firing the cycle wastes API budget. */
export function useTriggerSignalCycle(adminKey: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiPost<TriggerCycleResponse>('/api/admin/signals/trigger', undefined, {
        'X-Admin-Key': adminKey,
      }),
    retry: false,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin', 'metrics'] });
      // Cycle may have produced new signals / regime / dashboard counts.
      qc.invalidateQueries({ queryKey: ['dashboard'] });
      qc.invalidateQueries({ queryKey: ['signals'] });
      qc.invalidateQueries({ queryKey: ['regime'] });
    },
  });
}

/** Response shape of `POST /api/admin/signals/eval-outcomes`. Mirrors
 *  the per-tick summary `evaluate_pending_outcomes` returns on the backend.
 *  `remaining > 0` means the limit truncated the batch — operator should
 *  re-fire to drain the rest. `evaluated + skipped_* + errored == candidates`
 *  in every well-formed response. */
export interface EvalOutcomesResult {
  candidates: number;
  evaluated: number;
  skipped_no_direction: number;
  skipped_unknown_asset: number;
  skipped_no_klines: number;
  skipped_no_bars_in_window: number;
  errored: number;
  remaining: number;
}

/** Run the outcome evaluator on demand. Bounded by `limit` (server enforces
 *  [1, 100], default 20) so a click can't fire 100+ klines requests. Use
 *  the response's `remaining` to decide whether another click is needed.
 *  On success we invalidate `track-record` (the public hit-rate page reads
 *  SignalOutcome) and `signals` (signal status flips evaluated→evaluated). */
export function useEvalOutcomes(adminKey: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (limit: number = 20) =>
      apiPost<EvalOutcomesResult>(
        `/api/admin/signals/eval-outcomes?limit=${limit}`,
        undefined,
        { 'X-Admin-Key': adminKey },
      ),
    retry: false,
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ['admin', 'metrics'] });
      // Skip the wider invalidation when no row actually scored — saves
      // a few unnecessary refetches on empty-backlog clicks.
      if (result.evaluated > 0) {
        qc.invalidateQueries({ queryKey: ['track-record'] });
        qc.invalidateQueries({ queryKey: ['signals'] });
        qc.invalidateQueries({ queryKey: ['dashboard'] });
      }
    },
  });
}

/** Per-row failure detail emitted by `POST /api/admin/signals/retry-ai`.
 *  Mirrors `RetryAiErrorSample` on the backend. Capped to 3 entries by
 *  the server. */
export interface RetryAiErrorSample {
  signal_id: number;
  kind: string;
  detail: string;
}

/** Response shape of `POST /api/admin/signals/retry-ai`. `updated + failed
 *  == scanned` in every well-formed response; operators re-fire until
 *  `scanned === 0` to drain the backlog. */
export interface RetryAiResult {
  scanned: number;
  updated: number;
  failed: number;
  error_samples: RetryAiErrorSample[];
}

/** Re-run AI enrichment on Signals stranded with NULL `ai_analysis` (the
 *  backfill that the daily cycle deliberately doesn't perform — D12 only
 *  enriches NEWLY-inserted rows). Bounded by `limit` (server enforces
 *  [1, 50]) so a click can't drain OpenRouter quota on a large backlog.
 *  Successful enrichment unblocks fan-out (NULL-confidence signals are
 *  skipped by the delivery worker), so we invalidate dashboard + signals
 *  caches in addition to admin metrics. */
export function useRetryAiNullSignals(adminKey: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (limit: number = 10) =>
      apiPost<RetryAiResult>(
        `/api/admin/signals/retry-ai?limit=${limit}`,
        undefined,
        { 'X-Admin-Key': adminKey },
      ),
    retry: false,
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ['admin', 'metrics'] });
      // Only invalidate downstream caches when at least one row enriched —
      // a 0-update click means the backlog is empty or every row failed,
      // and the dashboard / signals lists haven't changed shape.
      if (result.updated > 0) {
        qc.invalidateQueries({ queryKey: ['dashboard'] });
        qc.invalidateQueries({ queryKey: ['signals'] });
      }
    },
  });
}

/** Response shape of `POST /api/admin/telegram/rotate-webhook-secret`. */
export interface RotateWebhookSecretResult {
  secret: string;
  note: string;
}

/** Rotate the Telegram webhook secret. One-time disclosure of the new
 *  value — operator MUST mirror it into the deploy env before the next
 *  container restart. See backend `rotate_webhook_secret` docstring for
 *  the race-free widen→push→shrink protocol. */
export function useRotateWebhookSecret(adminKey: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiPost<RotateWebhookSecretResult>(
        '/api/admin/telegram/rotate-webhook-secret',
        undefined,
        { 'X-Admin-Key': adminKey },
      ),
    retry: false,
    onSuccess: () => {
      // accepted_webhook_secrets count changes after rotation.
      qc.invalidateQueries({ queryKey: ['admin', 'metrics'] });
    },
  });
}

/** Single signal by ID. `enabled: id !== undefined` so the hook is safe to
 * call when a route param hasn't resolved yet (returns disabled query
 * instead of fetching `/api/signals/undefined`). */
export function useSignal(id: number | undefined) {
  return useQuery({
    queryKey: ['signals', id],
    queryFn: () => apiGet<SignalDetail>(`/api/signals/${id}`),
    enabled: id !== undefined,
  });
}
