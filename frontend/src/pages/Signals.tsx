import { useMemo, useState } from 'react';
import { useInfiniteSignals } from '../api/queries';
import type { SignalFilters } from '../api/types';
import { FilterBar, SignalCard } from '../components/signals';
import {
  Button,
  EmptyState,
  PageHeader,
  SkeletonCard,
} from '../components/ui';
import { formatAgo } from '../lib/format';

/**
 * /signals feed page — matches wireframe `src/screen-signals.jsx`.
 *
 * Layout (desktop):
 *   ┌─────────────────────────────────────────┐
 *   │ TopNav (in App shell)                   │
 *   ├─────────────────────────────────────────┤ ← sticky FilterBar begins
 *   │ Asset · Type · MinConf · Sort          │
 *   ├─────────────────────────────────────────┤
 *   │    Signal feed     Showing X of Y      │
 *   │    [SignalCard full-size]              │
 *   │    [SignalCard full-size]              │
 *   │    ...                                  │
 *   │    [Load 20 more]                       │
 *   └─────────────────────────────────────────┘
 *
 * Single-column max-w-[900px], NOT the 3-col grid used on home. Mock is
 * explicit — feed is a reading list, home's "most recent" is a teaser.
 */
export function Signals() {
  const [filters, setFilters] = useState<SignalFilters>({ limit: 20 });

  const query = useInfiniteSignals(filters);

  const items = useMemo(
    () => query.data?.pages.flatMap((p) => p.items) ?? [],
    [query.data],
  );

  // No total-count in the cursor-paginated API; show loaded count with a
  // "+" indicator when more pages remain. Matches wireframe's "Showing X"
  // spirit without faking a number we don't have.
  const meta = query.isLoading
    ? 'Loading…'
    : items.length === 0
      ? '0 results'
      : `Showing ${items.length}${query.hasNextPage ? '+' : ''} · updated ${formatAgo(new Date(query.dataUpdatedAt).toISOString())}`;

  const clearFilters = () =>
    setFilters({ limit: filters.limit });

  const hasActiveFilters = Boolean(
    filters.asset || filters.signal_type || (filters.confidence_min ?? 1) > 1,
  );

  return (
    <>
      <FilterBar value={filters} onChange={setFilters} />

      <section className="px-6 sm:px-8 py-7 pb-14">
        <div className="mx-auto max-w-[900px]">
          <PageHeader title="Signal feed" meta={meta} className="mb-[18px]" />

          {query.isLoading ? (
            <FeedLoading />
          ) : query.isError ? (
            <EmptyState
              title="Couldn't load signals."
              hint="Check your connection and retry."
              action={
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => query.refetch()}
                >
                  Retry
                </Button>
              }
            />
          ) : items.length === 0 ? (
            <EmptyState
              title={hasActiveFilters ? 'No signals match your filters.' : 'No signals yet.'}
              hint={
                hasActiveFilters
                  ? 'Try loosening the confidence floor or clearing the asset/type filter.'
                  : 'Check back after the next daily cycle (04:30 UTC).'
              }
              action={
                hasActiveFilters ? (
                  <Button variant="secondary" size="sm" onClick={clearFilters}>
                    Clear filters
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <>
              <div className="flex flex-col gap-3">
                {items.map((s) => (
                  <SignalCard key={s.id} signal={s} />
                ))}
              </div>

              <div className="flex justify-center mt-6">
                {query.hasNextPage ? (
                  <Button
                    variant="secondary"
                    size="md"
                    onClick={() => query.fetchNextPage()}
                    disabled={query.isFetchingNextPage}
                  >
                    {query.isFetchingNextPage ? 'Loading…' : 'Load 20 more'}
                  </Button>
                ) : (
                  <span className="font-mono text-[11px] text-text-4 uppercase tracking-[0.1em]">
                    end of feed
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      </section>
    </>
  );
}

function FeedLoading() {
  return (
    <div className="flex flex-col gap-3">
      {[0, 1, 2, 3, 4].map((i) => (
        <SkeletonCard key={i} />
      ))}
    </div>
  );
}
