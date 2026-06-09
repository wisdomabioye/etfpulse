import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { useSignals } from '../api/queries';
import type { SignalFilters } from '../api/types';
import { SignalFilterBar } from '../components/signals/SignalFilterBar';
import { SignalRow } from '../components/signal';
import { Button, EmptyState, LiveDot, Pager, Skeleton } from '../components/ui';

/**
 * /signals — dense, filterable, live feed (R6 reskin). Reuses the real
 * `useSignals` wiring (offset pagination, `SignalFilters`) and renders the
 * new `SignalRow` identity. The filter bar is the reskinned
 * `SignalFilterBar`; changing any filter resets to page 1.
 */
const PAGE_SIZE = 14;

export function Signals() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<SignalFilters>({ limit: PAGE_SIZE });

  const handleFiltersChange = (next: SignalFilters) => {
    setFilters(next);
    setPage(1);
  };

  const query = useSignals({ ...filters, page });
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const totalPages = query.data?.total_pages ?? 0;

  const hasActiveFilters = Boolean(
    filters.asset ||
      filters.signal_type ||
      (filters.confidence_min ?? 1) > 1 ||
      filters.include_expired ||
      (filters.sort && filters.sort !== 'newest'),
  );

  const clearFilters = () => {
    setFilters({ limit: filters.limit });
    setPage(1);
  };

  return (
    <>
      <SignalFilterBar value={filters} onChange={handleFiltersChange} />

      <div className="px-6 sm:px-8 py-7 pb-14 max-w-[1200px] mx-auto">
        <div className="flex items-baseline justify-between mb-4">
          <h1 className="text-[22px] font-semibold tracking-[-0.015em]">Signal feed</h1>
          <span className="font-mono text-[11px] text-t3 inline-flex items-center gap-1.5">
            {query.isLoading ? 'loading…' : `${total} match · showing ${items.length}`}
            <span>·</span>
            <LiveDot />
            polling 30s
          </span>
        </div>

        {query.isLoading ? (
          <div className="flex flex-col gap-3">
            {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
              <Skeleton key={i} className="h-[70px] w-full" />
            ))}
          </div>
        ) : query.isError ? (
          <EmptyState
            title="Couldn't load signals."
            hint="Check your connection and retry."
            action={
              <Button variant="secondary" size="sm" onClick={() => query.refetch()}>
                Retry
              </Button>
            }
          />
        ) : items.length === 0 ? (
          <EmptyState
            title={hasActiveFilters ? 'No signals match these filters' : 'No signals yet.'}
            hint={
              hasActiveFilters
                ? 'Try widening the confidence range or including expired signals.'
                : 'Check back shortly — new signals are picked up throughout the day.'
            }
            action={
              hasActiveFilters ? (
                <Button variant="outline" size="sm" onClick={clearFilters}>
                  Reset filters
                </Button>
              ) : undefined
            }
          />
        ) : (
          <>
            <div className="flex flex-col gap-2.5">
              {items.map((s) => (
                <SignalRow key={s.id} signal={s} onClick={() => navigate(`/signals/${s.id}`)} />
              ))}
            </div>
            <div className="mt-6">
              <Pager page={page} totalPages={totalPages} onPageChange={setPage} />
            </div>
          </>
        )}
      </div>
    </>
  );
}
