import { ApiError } from '../api/client';
import { useRegime } from '../api/queries';
import { RegimeCard, RegimeReasoning } from '../components/regime';
import {
  Button,
  EmptyState,
  Kicker,
  PageHeader,
  SectionLabel,
  Skeleton,
} from '../components/ui';

/**
 * /regime — current Wyckoff classification + structured score breakdown.
 *
 * Two states besides the happy path:
 *   - 503 from `/api/regime` ("regime not yet classified") → EmptyState
 *     directing to the next daily cycle. The backend returns 503 for both
 *     "table is empty" (cold-boot) and "latest snapshot is a legacy
 *     pre-Stage-7 row with NULL regime" — frontend surfaces them identically
 *     because the user-facing answer is the same: wait for the next cycle.
 *     `useRegime` short-circuits retry on 503 so we don't burn requests.
 *   - 5xx / network → Couldn't-load EmptyState with a manual retry button.
 *     The hook still does its one default retry on these (transient) errors;
 *     the button is for the case where that retry also fails.
 *
 * No routing-side error boundary; this page handles its own error states
 * inline so the TopNav stays mounted.
 */
export function Regime() {
  const query = useRegime();

  return (
    <div className="max-w-[920px] mx-auto px-6 sm:px-8 pt-8 pb-16">
      <PageHeader
        eyebrow={
          <Kicker dot dotColor="info">
            Market regime · Wyckoff phase
          </Kicker>
        }
        title="Current regime"
        meta={query.data ? <span>Live snapshot</span> : null}
      />

      <div className="mt-6">
        {query.isLoading ? (
          <RegimeLoading />
        ) : query.isError ? (
          query.error instanceof ApiError && query.error.status === 503 ? (
            <EmptyState
              title="Regime not yet classified."
              hint="The classifier runs once per daily cycle (04:30 UTC). Check back after the next run."
            />
          ) : (
            <EmptyState
              title="Couldn't load regime."
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
          )
        ) : query.data ? (
          <>
            <RegimeCard regime={query.data} />

            <section className="mt-10">
              <SectionLabel n="01">Score breakdown</SectionLabel>
              <RegimeReasoning reasoning={query.data.reasoning} />
            </section>

            <section className="mt-10">
              <SectionLabel n="02">How this is computed</SectionLabel>
              <div className="border border-border-2 bg-bg-2 rounded-lg px-5 py-4 text-[13px] leading-[1.65] text-text-2">
                <p className="m-0 mb-2">
                  Regime is a composite score over three inputs: 7-day net
                  ETF flow trend (BTC + ETH combined), 24-hour news velocity,
                  and macro events nearby (±N days from today).
                </p>
                <p className="m-0">
                  The signed combined score maps to a Wyckoff regime
                  (accumulation, markup, distribution, markdown, uncertain)
                  and a posture (aggressive, normal, cautious, paused) that
                  governs how aggressively new signals are fanned out.
                </p>
              </div>
            </section>
          </>
        ) : null}
      </div>
    </div>
  );
}

function RegimeLoading() {
  return (
    <div className="flex flex-col gap-4">
      <Skeleton className="h-32 w-full" />
      <Skeleton className="h-6 w-48" />
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3.5">
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    </div>
  );
}
