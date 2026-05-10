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

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { ApiError, apiGet } from './client';
import type {
  AdminMetrics,
  DashboardStats,
  PaginatedSignals,
  PaginatedTrackRecord,
  RegimeResponse,
  SignalDetail,
  SignalFilters,
  TrackRecordFilters,
} from './types';

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
