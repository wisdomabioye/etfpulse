/**
 * TanStack Query hooks — one per backend endpoint (public/read surface).
 *
 * Pages consume `useDashboardStats()`, `useSignals()`, `useInfiniteSignals()`,
 * `useSignal(id)`. Loading + error + caching + dedup are TanStack defaults
 * (configured in main.tsx: 30s staleTime, retry once, no refetch-on-focus).
 *
 * Query-key convention: `['<resource>', ...specifiers]`. Stable across
 * re-renders — TanStack deep-compares so identical filter VALUES (even on
 * fresh object references) hit the cache.
 */

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { ApiError, apiGet } from './client';
import type {
  CalibrationResponse,
  PerDetectorResponse,
  DashboardStats,
  PaginatedSignals,
  PaginatedTrackRecord,
  RegimeHistoryResponse,
  RegimeResponse,
  SignalDetail,
  SignalFilters,
  SpotPrices,
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

/** Live BTC + ETH spot prices for the top-nav strip.
 *
 * Backend's in-process cache TTL is 30s, so refetching faster than that just
 * burns network roundtrips for the same payload. 60s here keeps the visible
 * price reasonably fresh while letting the cache absorb traffic from
 * multiple tabs. retry disabled — a transient blip recovers on the next
 * cadence tick and isn't worth an immediate re-bang. */
export function useSpotPrices() {
  return useQuery({
    queryKey: ['prices', 'spot'],
    queryFn: () => apiGet<SpotPrices>('/api/prices/spot'),
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: false,
    staleTime: 30_000,
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

/** Recent regime classifications (one per day) for the /regime history strip.
 *  Returns 200 + empty list when there are no snapshots, so it never 503s the
 *  page — the strip is supplementary. */
export function useRegimeHistory(days = 8) {
  return useQuery({
    queryKey: ['regime', 'history', days],
    queryFn: () => apiGet<RegimeHistoryResponse>(`/api/regime/history?days=${days}`),
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

/** PR I.1 — confidence calibration reliability curve.
 *
 *  Defaults to the active `AI_PROMPT_VERSION` server-side, so the FE
 *  doesn't have to coordinate the version on every page mount. Pass
 *  `ai_prompt_version` explicitly when comparing cohorts.
 *
 *  Backend caches the aggregation per (version, lookback, bucket_size,
 *  min_samples) for 5 min, so FE polling burst → ≤1 DB hit per window
 *  per worker. We mirror that with a 60s staleTime — long enough to
 *  ride a tab session without refetching, short enough that a manual
 *  page revisit gets a near-fresh number. retry off — cold-boot
 *  returns 200 with empty buckets (not 503), so any error here is
 *  config / network and won't self-fix on retry. */
export function useCalibration(params?: {
  ai_prompt_version?: string;
  lookback_days?: number;
}) {
  return useQuery({
    queryKey: [
      'track-record',
      'calibration',
      params?.ai_prompt_version,
      params?.lookback_days,
    ],
    queryFn: () =>
      apiGet<CalibrationResponse>('/api/track-record/calibration', params ?? {}),
    staleTime: 60_000,
    retry: false,
  });
}

/** PR I.3 — per-detector precision grid.
 *
 *  Defaults to the active `AI_PROMPT_VERSION` server-side, same convention
 *  as `useCalibration`. Pass `ai_prompt_version` explicitly when comparing
 *  cohorts.
 *
 *  Backend caches per (version, lookback, min_samples) for 5 min (shares
 *  `calibration_cache_ttl_seconds`); FE staleTime mirrors at 60s so a tab
 *  session doesn't refetch while a deliberate page revisit still gets a
 *  near-fresh number. retry off — cold-boot returns 200 with all-zero
 *  cells (not 503), so any error is config / network and won't self-fix.
 *
 *  The card on `/track-record` filters out regime_shift by design — the
 *  backend already excludes it (PR I.3b will fold it in once MARKET
 *  composite scoring lands). The hook stays detector-agnostic so a future
 *  per-detector drill-down page can reuse it. */
export function usePerDetector(params?: {
  ai_prompt_version?: string;
  lookback_days?: number;
}) {
  return useQuery({
    queryKey: [
      'track-record',
      'per-detector',
      params?.ai_prompt_version,
      params?.lookback_days,
    ],
    queryFn: () =>
      apiGet<PerDetectorResponse>('/api/track-record/per-detector', params ?? {}),
    staleTime: 60_000,
    retry: false,
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
