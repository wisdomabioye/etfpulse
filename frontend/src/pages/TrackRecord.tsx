import { Link } from 'react-router-dom';
import { useState } from 'react';
import { useCalibration, usePerDetector, useTrackRecord } from '../api/queries';
import type {
  AssetSymbol,
  HorizonLabel,
  SignalType,
  TrackRecordFilters,
  TrackRecordItem,
  TrackRecordSummary,
} from '../api/types';
import {
  Button,
  EmptyState,
  Kicker,
  PageHeader,
  Pager,
  Skeleton,
  StatTile,
} from '../components/ui';
import {
  ReliabilityChart,
  ReliabilityChartSkeleton,
} from '../components/track-record/ReliabilityChart';
import {
  DetectorPrecisionCard,
  DetectorPrecisionCardSkeleton,
} from '../components/track-record/DetectorPrecisionCard';
import { formatAgo, formatSignalType } from '../lib/format';

/**
 * /track-record — public outcomes table + same-filter summary stats.
 *
 * Stage 8-P6. Companion to the home-page HeroHitRatePanel: home shows the
 * GLOBAL number; this page shows the same number filtered by the user's
 * current selection (asset / signal_type / confidence_min) so "BTC track
 * record" makes sense.
 *
 * Layout: PageHeader → 4-tile summary card → inline filter row →
 * outcome list (color-coded: green=hit, red=stop, gray=neither/no-target).
 *
 * Pagination: page-numbered (offset). 10 per page. Cursor mode is also
 * available on the endpoint but the page UI is built for numbered nav.
 *
 * Empty state: total_evaluated=0 → "first outcomes land 72h after a
 * signal fires" — same copy as the empty HeroHitRatePanel for consistency
 * (one truth: outcomes are 72h-delayed by design).
 */
const PAGE_SIZE = 10;

const SIGNAL_TYPES: SignalType[] = [
  'flow_anomaly',
  'magnitude',
  'acceleration',
  'divergence',
  'regime_shift',
];

const SELECT_CLASS =
  'bg-bg-3 text-text-1 border border-border-3 rounded-[5px] px-[10px] py-[7px] text-[12px] font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 disabled:cursor-not-allowed';

const LABEL =
  'font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] select-none';

export function TrackRecord() {
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<TrackRecordFilters>({ limit: PAGE_SIZE });

  const handleFiltersChange = (next: TrackRecordFilters) => {
    setFilters(next);
    setPage(1); // narrowing filters → reset to page 1, same as /signals
  };

  const query = useTrackRecord({ ...filters, page });
  // PR I.1 — reliability chart. Independent of the page's filters (the
  // chart's bucket-by-confidence view is across-the-board, not per-asset
  // or per-detector) so the user sees a stable cohort view as they
  // explore the list. Falls back to a skeleton on first paint; the
  // component handles empty/insufficient-cell rendering itself.
  const calibrationQuery = useCalibration();
  // PR I.3 — per-detector precision card. Independent of the page's
  // filters for the same reason as calibration: the leaderboard is a
  // cohort-level view, not a slice. regime_shift is excluded by the
  // backend pending PR I.3b (MARKET composite scoring).
  const perDetectorQuery = usePerDetector();

  const items = query.data?.items ?? [];
  const totalPages = query.data?.total_pages ?? 0;
  const summary = query.data?.summary ?? null;

  const hasActiveFilters = Boolean(
    filters.asset ||
      filters.signal_type ||
      filters.horizon ||
      (filters.confidence_min ?? 1) > 1,
  );

  const clearFilters = () => {
    setFilters({ limit: filters.limit });
    setPage(1);
  };

  return (
    <div className="max-w-[920px] mx-auto px-6 sm:px-8 pt-8 pb-16">
      <PageHeader
        eyebrow={
          <Kicker dot dotColor="pos">
            Public track record · hit rate by horizon
          </Kicker>
        }
        title="Track record"
        meta={
          summary
            ? `${summary.total_evaluated} evaluated · updated ${formatAgo(new Date(query.dataUpdatedAt).toISOString())}`
            : null
        }
      />

      {query.isLoading ? (
        <TrackRecordLoading />
      ) : query.isError ? (
        <div className="mt-6">
          <EmptyState
            title="Couldn't load track record."
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
        </div>
      ) : summary ? (
        <>
          <SummaryGrid summary={summary} className="mt-6" />
          <HorizonBucketGrid summary={summary} className="mt-3" />

          {/* PR I.1 — reliability curve. Sits between the summary stats
              (the "what is our hit rate?" tiles) and the filter row
              (the "drill in" controls) because it answers a higher-order
              question: do the confidence scores mean what they say?
              Renders independently of the list filters — the calibration
              cohort is fixed at the active prompt version, not sliced. */}
          {calibrationQuery.isLoading ? (
            <ReliabilityChartSkeleton className="mt-8" />
          ) : calibrationQuery.data ? (
            <ReliabilityChart data={calibrationQuery.data} className="mt-8" />
          ) : null}

          {/* PR I.3 — per-detector precision leaderboard. Sits BELOW the
              calibration chart (which asks "is confidence honest?") and
              ABOVE the filter row (the "drill in" controls) because it
              answers the natural follow-up: "which detector should I
              trust?" Same cohort grouping as calibration — fixed at the
              active prompt version, not sliced by list filters. */}
          {perDetectorQuery.isLoading ? (
            <DetectorPrecisionCardSkeleton className="mt-8" />
          ) : perDetectorQuery.data ? (
            <DetectorPrecisionCard data={perDetectorQuery.data} className="mt-8" />
          ) : null}

          <FilterRow
            value={filters}
            onChange={handleFiltersChange}
            className="mt-8"
          />

          {items.length === 0 ? (
            <div className="mt-6">
              <EmptyState
                title={
                  hasActiveFilters
                    ? 'No outcomes match your filters.'
                    : 'No outcomes evaluated yet.'
                }
                hint={
                  hasActiveFilters
                    ? 'Try loosening the confidence floor or clearing the asset/type/horizon filter.'
                    : 'First outcomes land once signals complete their validity window — the evaluator runs hourly after that.'
                }
                action={
                  hasActiveFilters ? (
                    <Button variant="secondary" size="sm" onClick={clearFilters}>
                      Clear filters
                    </Button>
                  ) : undefined
                }
              />
            </div>
          ) : (
            <>
              <ul className="mt-6 m-0 p-0 list-none flex flex-col gap-2">
                {items.map((item) => (
                  <OutcomeRow key={item.id} item={item} />
                ))}
              </ul>

              <div className="mt-6">
                <Pager page={page} totalPages={totalPages} onPageChange={setPage} />
              </div>
            </>
          )}
        </>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Summary stat grid — 4 tiles
// ---------------------------------------------------------------------------

function SummaryGrid({
  summary,
  className,
}: {
  summary: TrackRecordSummary;
  className?: string;
}) {
  // Hit rate is in percent (0..100) per the API contract.
  const hitRate =
    summary.hit_rate_pct !== null ? `${Math.round(summary.hit_rate_pct)}%` : '—';

  // Avg-confidence delta — high signal: hits had 8.4 confidence, misses
  // had 5.1 → "+3.3 vs misses". Empty when either bucket is empty.
  const confDeltaText =
    summary.avg_confidence_hits !== null && summary.avg_confidence_misses !== null
      ? `${summary.avg_confidence_hits.toFixed(1)} vs ${summary.avg_confidence_misses.toFixed(1)}`
      : summary.avg_confidence_hits !== null
        ? summary.avg_confidence_hits.toFixed(1)
        : '—';

  return (
    <div
      className={`grid grid-cols-2 md:grid-cols-4 gap-2.5 ${className ?? ''}`.trim()}
    >
      <StatTile label="Total evaluated" value={summary.total_evaluated} />
      {/* PR B (#60) — renamed from "Hit rate (72h)" because the v2 rubric
          scores each signal against its OWN window. Per-horizon breakdown
          lives in <HorizonBucketGrid> right below this tile. */}
      <StatTile label="Hit rate" value={hitRate} />
      <StatTile
        label="Hits / Stops"
        value={`${summary.targets_hit} / ${summary.stops_hit}`}
      />
      <StatTile label="Avg conf · hits/misses" value={confDeltaText} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Horizon bucket grid — PR B (#60) per-bucket hit rate, side-by-side
// ---------------------------------------------------------------------------

const HORIZON_DESCRIPTIONS: Record<HorizonLabel, string> = {
  scalp: 'scalp · ~6h',
  swing: 'swing · ~72h',
  position: 'position · ~7d',
  legacy: 'legacy · 72h fixed',
};

function HorizonBucketGrid({
  summary,
  className,
}: {
  summary: TrackRecordSummary;
  className?: string;
}) {
  // Bucketed hit-rate from `/api/track-record.summary.hit_rate_by_horizon`.
  // The backend ALWAYS emits all four keys; FE renders them in fixed order
  // (scalp → swing → position → legacy) so the row reads chronologically
  // by horizon length, then the legacy tail.
  const order: HorizonLabel[] = ['scalp', 'swing', 'position', 'legacy'];
  return (
    <div
      className={`grid grid-cols-2 md:grid-cols-4 gap-2.5 ${className ?? ''}`.trim()}
    >
      {order.map((label) => {
        const pct = summary.hit_rate_by_horizon[label];
        // Null = empty bucket (no scored signals). Render "—" + the
        // horizon descriptor so the empty tile still tells the story
        // ("scalp · pending intraday data" et al). Distinguishes from
        // "0% hit rate" which means "we scored 5 of them and none hit."
        const value = pct === null ? '—' : `${Math.round(pct)}%`;
        return (
          <StatTile
            key={label}
            label={HORIZON_DESCRIPTIONS[label]}
            value={value}
          />
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inline filter row — asset / type / min-confidence
// ---------------------------------------------------------------------------

function FilterRow({
  value,
  onChange,
  className,
}: {
  value: TrackRecordFilters;
  onChange: (next: TrackRecordFilters) => void;
  className?: string;
}) {
  const asset = value.asset ?? 'ALL';
  const type = value.signal_type ?? 'ALL';
  const horizon = value.horizon ?? 'ALL';
  const confMin = value.confidence_min ?? 1;

  // Match /signals's normalization — `undefined` is the "off" sentinel
  // (apiGet drops undefined values from the query string).
  const setAsset = (next: AssetSymbol | 'ALL') =>
    onChange({ ...value, asset: next === 'ALL' ? undefined : next });

  const setType = (next: SignalType | 'ALL') =>
    onChange({ ...value, signal_type: next === 'ALL' ? undefined : next });

  const setHorizon = (next: HorizonLabel | 'ALL') =>
    onChange({ ...value, horizon: next === 'ALL' ? undefined : next });

  const setConfMin = (next: number) =>
    onChange({ ...value, confidence_min: next <= 1 ? undefined : next });

  return (
    <div
      className={`flex flex-wrap items-center gap-3 px-4 py-3 border border-border-2 rounded-md bg-bg-2 ${className ?? ''}`.trim()}
    >
      <div className="flex items-center gap-2">
        <span className={LABEL}>Asset</span>
        <select
          className={SELECT_CLASS}
          value={asset}
          onChange={(e) => setAsset(e.target.value as AssetSymbol | 'ALL')}
          aria-label="Filter by asset"
        >
          <option value="ALL">All</option>
          <option value="BTC">BTC</option>
          <option value="ETH">ETH</option>
        </select>
      </div>

      <div className="flex items-center gap-2">
        <span className={LABEL}>Type</span>
        <select
          className={SELECT_CLASS}
          value={type}
          onChange={(e) => setType(e.target.value as SignalType | 'ALL')}
          aria-label="Filter by signal type"
        >
          <option value="ALL">All</option>
          {SIGNAL_TYPES.map((t) => (
            <option key={t} value={t}>
              {formatSignalType(t)}
            </option>
          ))}
        </select>
      </div>

      <div className="flex items-center gap-2">
        <span className={LABEL}>Horizon</span>
        <select
          className={SELECT_CLASS}
          value={horizon}
          onChange={(e) => setHorizon(e.target.value as HorizonLabel | 'ALL')}
          aria-label="Filter by horizon"
        >
          <option value="ALL">All</option>
          <option value="scalp">Scalp</option>
          <option value="swing">Swing</option>
          <option value="position">Position</option>
          <option value="legacy">Legacy</option>
        </select>
      </div>

      <div className="flex items-center gap-2">
        <span className={LABEL}>Min conf</span>
        <select
          className={SELECT_CLASS}
          value={String(confMin)}
          onChange={(e) => setConfMin(Number(e.target.value))}
          aria-label="Minimum confidence"
        >
          {[1, 4, 7, 9].map((n) => (
            <option key={n} value={n}>
              {n === 1 ? 'Any' : `${n}+`}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Outcome row — color-coded by hit/stop/neither
// ---------------------------------------------------------------------------

function OutcomeRow({ item }: { item: TrackRecordItem }) {
  // Color band: green for hit_target, red for hit_stop, muted for neither
  // and for no-target signals. `hit_target=true` wins over `hit_stop=true`
  // — a signal that traded both target and stop within 72h is a "hit" for
  // public-display purposes (the more user-favorable read).
  const tone: 'pos' | 'neg' | 'muted' =
    item.hit_target === true
      ? 'pos'
      : item.hit_stop === true
        ? 'neg'
        : 'muted';

  const toneClasses = {
    pos: 'border-l-2 border-l-pos',
    neg: 'border-l-2 border-l-neg',
    muted: 'border-l-2 border-l-text-4',
  } as const;

  const outcomeBadge =
    item.hit_target === true
      ? <span className="text-pos">✓ target</span>
      : item.hit_stop === true
        ? <span className="text-neg">✗ stop</span>
        : item.hit_target === null
          ? <span className="text-text-4">— no target</span>
          : <span className="text-text-3">— neither</span>;

  // Returns over the signal's NATIVE window — `price_at_validity_end`
  // (PR B v2 fact) with `price_after_72h` fallback for legacy rows.
  // Same baseline rules as the backend evaluator's `entry_for_metrics`
  // (see `pipeline/track_record.py:_evaluate_one`) AND as `OutcomeCard`'s
  // `entryBaseline`. Diverging would mean this list cell shows a
  // different number than the detail page's outcome row for the same
  // outcome AND would contradict the verdict's hit_target math when
  // entry_price ≠ price_at_signal.
  //
  // For position signals (168h window), `price_at_validity_end` is the
  // 7-day close — the correct outcome price; pre-PR-B the row would
  // show the 72h close instead (a misleading mid-trade reading).
  const baseline = item.entry_price ?? item.price_at_signal;
  const closeAtEnd =
    item.price_at_validity_end ?? item.price_after_72h;
  const pctReturn =
    closeAtEnd !== null && baseline > 0
      ? ((closeAtEnd - baseline) / baseline) * 100
      : null;
  const pctText =
    pctReturn !== null ? `${pctReturn >= 0 ? '+' : ''}${pctReturn.toFixed(2)}%` : '—';
  const pctClass =
    pctReturn === null ? 'text-text-4' : pctReturn >= 0 ? 'text-pos' : 'text-neg';

  const assetLink = (
    <Link
      to={`/signals/${item.signal_id}`}
      className="hover:text-accent transition-colors"
    >
      {item.asset}
    </Link>
  );
  const typeLabel = formatSignalType(item.signal_type);
  const ago = formatAgo(item.evaluated_at);

  return (
    <li className={`px-4 py-3 bg-bg-2 rounded-md ${toneClasses[tone]}`}>
      {/* ----------------------------- Mobile (<md) -----------------------------
          Two rows. Top row gives the actionable answer at a glance: asset,
          verdict, % return. Bottom row carries the metadata strip: type ·
          direction · confidence · time-since-eval. Splitting at md keeps
          everything readable on a 360px viewport without sacrificing any
          column from the desktop layout. */}
      <div className="md:hidden flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <div className="text-[14px] text-text-1 font-semibold">{assetLink}</div>
          <div className="flex items-center gap-3 font-mono text-[12px] tabular-nums">
            <span className="text-[11px]">{outcomeBadge}</span>
            <span className={pctClass}>{pctText}</span>
          </div>
        </div>
        <div className="flex items-center justify-between gap-2 font-mono text-[10px] text-text-3 uppercase tracking-[0.08em]">
          <div className="truncate">
            {typeLabel} · {item.direction} · conf {item.confidence}/10
          </div>
          <div className="text-text-4 shrink-0">{ago}</div>
        </div>
      </div>

      {/* ----------------------------- Desktop (≥md) ----------------------------
          Original 5-column layout — unchanged. Fixed widths align the
          columns visually across rows so a reader can scan down a single
          dimension (e.g. "all the +X% returns") without re-tracking. */}
      <div className="hidden md:flex md:items-center md:gap-4">
        <div className="w-24 shrink-0">
          <div className="text-[13px] text-text-1 font-semibold">{assetLink}</div>
          <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.08em]">
            {typeLabel}
          </div>
        </div>
        <div className="w-24 shrink-0">
          <div className="text-[12px] text-text-2 uppercase">{item.direction}</div>
          <div className="font-mono text-[11px] text-text-3">
            conf {item.confidence}/10
          </div>
        </div>
        <div className="w-28 shrink-0 font-mono text-[11px] tabular-nums">
          {outcomeBadge}
        </div>
        <div className={`w-20 shrink-0 font-mono text-[12px] tabular-nums ${pctClass}`}>
          {pctText}
        </div>
        <div className="flex-1" />
        <div className="font-mono text-[10px] text-text-4 tabular-nums shrink-0">
          {ago}
        </div>
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Loading skeleton
// ---------------------------------------------------------------------------

function TrackRecordLoading() {
  return (
    <div className="mt-6 flex flex-col gap-4">
      {/* Top row: matches `<SummaryGrid>` — 4 stat tiles (total evaluated,
          flat hit rate, hits/stops, avg confidence). */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-20 w-full" />
        ))}
      </div>
      {/* PR B (#60) — second row matches `<HorizonBucketGrid>`: 4 bucket
          tiles (scalp / swing / position / legacy). Mirrors the real
          layout's 8-tile total so the data-load transition doesn't pop
          extra tiles into view. */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={`b${i}`} className="h-20 w-full" />
        ))}
      </div>
      <div className="flex flex-col gap-2 mt-4">
        {[0, 1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-14 w-full" />
        ))}
      </div>
    </div>
  );
}
