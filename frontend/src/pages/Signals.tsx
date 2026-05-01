import { useState } from 'react';
import { useSignals } from '../api/queries';
import type { SignalFilters } from '../api/types';
import { FilterBar, SignalCard } from '../components/signals';
import {
  Button,
  EmptyState,
  PageHeader,
  Pager,
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
 *   │    Signal feed     Showing X-Y of Z    │
 *   │    [SignalCard full-size]              │
 *   │    [SignalCard full-size]              │
 *   │    ...                                  │
 *   │    Prev · 1 · 2 · 3 · … · N · Next     │
 *   └─────────────────────────────────────────┘
 *
 * Single-column max-w-[900px], NOT the 3-col grid used on home. Mock is
 * explicit — feed is a reading list, home's "most recent" is a teaser.
 *
 * Pagination: page-numbered (offset-based). 10 cards per page; the API
 * returns `total`, `page`, `total_pages` so the Pager renders exact counts.
 * Page state lives in this component; URL-syncing is a separate concern
 * (TODO open issue if shareable links matter).
 */
const PAGE_SIZE = 10;

export function Signals() {
  // Page state is separate from filters so changing a filter resets to page 1
  // (more intuitive than landing on "page 5 of 1" results when narrowing).
  // Reset is wired in `handleFiltersChange` below — doing it in useEffect
  // would violate react-hooks/set-state-in-effect.
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
  const limit = filters.limit ?? PAGE_SIZE;

  // "Showing 11-20 of 47" — exact range. Falls back gracefully on the
  // last (partial) page where end < page * limit.
  const rangeStart = total === 0 ? 0 : (page - 1) * limit + 1;
  const rangeEnd = Math.min(page * limit, total);

  const meta = query.isLoading
    ? 'Loading…'
    : total === 0
      ? '0 results'
      : `Showing ${rangeStart}-${rangeEnd} of ${total} · updated ${formatAgo(new Date(query.dataUpdatedAt).toISOString())}`;

  const clearFilters = () => {
    setFilters({ limit: filters.limit });
    setPage(1);
  };

  // "Active" means: anything that narrows the result set away from the
  // implicit defaults (asset/type/confidence-floor/include-expired/sort).
  // Drives the empty-state copy + the "Clear filters" affordance — so any
  // filter that could explain a 0-result page belongs here.
  const hasActiveFilters = Boolean(
    filters.asset ||
      filters.signal_type ||
      (filters.confidence_min ?? 1) > 1 ||
      filters.include_expired ||
      (filters.sort && filters.sort !== 'newest'),
  );

  return (
    <>
      <FilterBar value={filters} onChange={handleFiltersChange} />

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

              <div className="mt-6">
                <Pager
                  page={page}
                  totalPages={totalPages}
                  onPageChange={setPage}
                />
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
