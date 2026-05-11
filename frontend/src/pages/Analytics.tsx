import { useAnalyticsBreakdown } from '../api/queries';
import type { BreakdownStat, SignalType } from '../api/types';
import { BreakdownSection } from '../components/analytics';
import { Container } from '../components/layout';
import { Callout, Histogram, PageHeader, Skeleton } from '../components/ui';
import { formatSignalType } from '../lib/format';

/**
 * `/analytics` — public diagnostic breakdown of the track record.
 *
 * Stage 8-P10. Complements `/track-record` (which is the list view + a
 * single global hit rate) by surfacing WHERE the hit rate comes from:
 * which detectors, which assets, which confidence buckets, which
 * directions, plus the shape of MFE/MAE distributions.
 *
 * Diagnostic intent — each section's caption is the question that
 * section answers. Readers should be able to scan the section titles
 * and know what they're about to learn without reading the table data
 * first.
 *
 * Cold-boot UX — the endpoint returns 200 with empty arrays before any
 * outcome evaluates. The page renders the full structure (titles,
 * captions, empty histograms with stable x-axes) so first-time visitors
 * see what the page WILL show, not a 503-shaped void.
 */
export function Analytics() {
  const { data, isLoading, error } = useAnalyticsBreakdown();

  if (isLoading) {
    return (
      <Container>
        <PageHeader title="Analytics" />
        <div className="mt-6">
          <Skeleton className="h-[400px]" />
        </div>
      </Container>
    );
  }

  if (error || !data) {
    return (
      <Container>
        <PageHeader title="Analytics" />
        <div className="mt-6">
          <Callout tone="neg">
            Couldn't load the analytics breakdown. Try refreshing — if the issue
            persists, the backend may be unreachable.
          </Callout>
        </div>
      </Container>
    );
  }

  return (
    <Container className="pt-10 pb-16">
      <PageHeader
        title="Analytics"
        meta={
          data.total_outcomes > 0
            ? `Based on ${data.total_outcomes} evaluated ${
                data.total_outcomes === 1 ? 'outcome' : 'outcomes'
              }`
            : 'No outcomes yet'
        }
      />

      <p className="mt-5 text-[14px] text-text-2 max-w-2xl leading-relaxed">
        Where does the track record come from? This page breaks the global hit
        rate down by detector, asset, confidence, and direction — plus the shape
        of how close near-misses got to target or stop.
      </p>

      {/* Categorical breakdowns — 2x2 grid on wide, stacked on narrow. */}
      <div className="mt-8 grid gap-4 md:grid-cols-2">
        <BreakdownSection
          title="By detector"
          caption="Which detector earns its compute?"
          rows={prettifyDetectorRows(data.by_detector)}
          labelHeader="Detector"
        />
        <BreakdownSection
          title="By asset"
          caption="Is one asset systematically easier?"
          rows={data.by_asset}
          labelHeader="Asset"
        />
        <BreakdownSection
          title="By confidence"
          caption="Is the AI confidence score calibrated? (Per-bucket, not cumulative.)"
          rows={data.by_confidence_bucket}
          labelHeader="Bucket"
          preserveOrder
        />
        <BreakdownSection
          title="By direction"
          caption="Is there a long/short bias in what the AI suggests?"
          rows={prettifyDirectionRows(data.by_direction)}
          labelHeader="Direction"
        />
      </div>

      {/* Distribution histograms — full-width, stacked. */}
      <section className="mt-8 bg-bg-2 border border-border-2 rounded-md p-5">
        <header className="mb-3">
          <h3 className="text-[15px] font-semibold text-text-1">
            Max favorable excursion (MFE)
          </h3>
          <p className="mt-1 text-[12px] text-text-3">
            How close did signals get to target? Big bars on the right = signals
            often ran far in our favor. Big bars on the left = entries didn't move.
          </p>
        </header>
        <Histogram
          buckets={data.mfe_histogram}
          ariaLabel="Distribution of maximum favorable price moves across all evaluated signals"
        />
      </section>

      <section className="mt-4 bg-bg-2 border border-border-2 rounded-md p-5">
        <header className="mb-3">
          <h3 className="text-[15px] font-semibold text-text-1">
            Max adverse excursion (MAE)
          </h3>
          <p className="mt-1 text-[12px] text-text-3">
            How close did signals get to stop? Big bars on the right = lots of
            near-stops, suggesting stops are too tight. Flat distribution = stops
            rarely tested.
          </p>
        </header>
        <Histogram
          buckets={data.mae_histogram}
          ariaLabel="Distribution of maximum adverse price moves across all evaluated signals"
        />
      </section>

      {/* Honest caveats. Read this if you're tempted to draw conclusions. */}
      <section className="mt-10 bg-bg-2 border border-border-2 rounded-md px-7 py-6">
        <h3 className="text-[12px] font-mono uppercase tracking-[0.1em] text-text-3 mb-4">
          Caveats
        </h3>
        <ul className="space-y-3 text-[13px] text-text-2 leading-relaxed list-disc pl-5">
          <li>
            Hit rate divides <span className="font-mono">hits / targeted</span>,
            not <span className="font-mono">hits / total</span> — signals where
            the AI declined to set a target don't pollute the denominator.
          </li>
          <li>
            Outcomes are measured against <strong>daily</strong> Binance bars
            over the 72h window after a signal fires. Intraday spikes that hit
            and reverse before the daily close are not captured.
          </li>
          <li>
            With a small sample, single-digit-cohort rows are noisy. Trust the
            shape, not the exact percent, until cohort sizes grow.
          </li>
          <li>
            Server caches this breakdown for 5 minutes — fresh outcomes appear
            on the next cache refresh.
          </li>
        </ul>
      </section>
    </Container>
  );
}

/** Prettify detector labels from snake_case to "Title Case". Backend stores
 *  the SignalType enum strings ("flow_anomaly"); the breakdown row label IS
 *  that string. We narrow + format here so the table reads as English.
 *
 *  Unknown values pass through untouched — the breakdown is descriptive,
 *  not enum-bound; a future detector that ships before the frontend types
 *  catch up should still render rather than crash. */
function prettifyDetectorRows(rows: BreakdownStat[]): BreakdownStat[] {
  return rows.map((r) => ({
    ...r,
    label: formatSignalType(r.label as SignalType),
  }));
}

/** Capitalize direction labels ("long" → "Long"). Direction values are
 *  fixed by the backend enum (long/short/neutral); the upper-casing is
 *  cosmetic. */
function prettifyDirectionRows(rows: BreakdownStat[]): BreakdownStat[] {
  return rows.map((r) => ({
    ...r,
    label: r.label.charAt(0).toUpperCase() + r.label.slice(1),
  }));
}
