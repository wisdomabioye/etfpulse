/**
 * Admin TanStack Query hooks — gated by `X-Admin-Key`.
 *
 * Metrics + on-demand pipeline triggers (signal cycle, outcome eval, AI
 * retry), delivery trace, and webhook-secret rotation. See `queries-admin-ops`
 * for the per-user / execution / symbols operations.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiGet, apiPost } from './client';
import type { AdminMetrics } from './types';

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

/** Response shape of `GET /api/admin/signals/{id}/delivery-trace`.
 *  Mirrors backend `DeliveryTrace`. */
export interface DeliveryTraceRecipient {
  kind: 'user' | 'group';
  target_id: number;
  target_label: string;
  chat_id: string | number | null;
  target_active: boolean;
  target_paused: boolean;
  channel_active: boolean | null;
  asset_match: boolean;
  confidence_match: boolean;
  matched: boolean;
  exclude_reason: string | null;
  delivery_status: string | null;
  delivery_attempts: number | null;
  delivery_error: string | null;
}

export interface DeliveryTraceResult {
  signal_id: number;
  signal_asset: string;
  signal_type: string;
  signal_confidence: number | null;
  signal_status: string;
  delivery_count: number;
  delivered_count: number;
  pending_count: number;
  failed_count: number;
  skipped_count: number;
  matched_count: number;
  recipients: DeliveryTraceRecipient[];
}

/** Fetch the delivery trace for one signal. Lazy — `enabled` gates so
 *  the hook is safe to mount with `signalId=null` before the operator
 *  has chosen one. Each fetch is a fresh DB roundtrip (no cache reuse
 *  by signalId — operator typically queries one at a time). #186. */
export function useDeliveryTrace(adminKey: string, signalId: number | null) {
  return useQuery({
    // `adminKey` in the key matches the `useAdminMetrics` pattern —
    // rotating the key invalidates the cache so a different operator
    // session sees fresh data, not the previous session's snapshot.
    queryKey: ['admin', 'delivery-trace', adminKey, signalId],
    queryFn: () =>
      apiGet<DeliveryTraceResult>(
        `/api/admin/signals/${signalId}/delivery-trace`,
        undefined,
        { 'X-Admin-Key': adminKey },
      ),
    enabled: adminKey.length > 0 && signalId !== null,
    retry: false,
    // Operator-initiated; don't auto-refetch on focus.
    refetchOnWindowFocus: false,
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
