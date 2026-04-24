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
import { apiGet } from './client';
import type {
  DashboardStats,
  PaginatedSignals,
  SignalDetail,
  SignalFilters,
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
