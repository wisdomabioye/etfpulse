import { Link } from 'react-router-dom';
import { useState } from 'react';
import { useTrackRecord } from '../api/queries';
import type {
  AssetSymbol,
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

  const items = query.data?.items ?? [];
  const totalPages = query.data?.total_pages ?? 0;
  const summary = query.data?.summary ?? null;

  const hasActiveFilters = Boolean(
    filters.asset || filters.signal_type || (filters.confidence_min ?? 1) > 1,
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
            Public track record · 72h hit rate
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
                    ? 'Try loosening the confidence floor or clearing the asset/type filter.'
                    : 'First outcomes land 72h after a signal fires — the evaluator runs hourly after that.'
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
      <StatTile label="Hit rate (72h)" value={hitRate} />
      <StatTile
        label="Hits / Stops"
        value={`${summary.targets_hit} / ${summary.stops_hit}`}
      />
      <StatTile label="Avg conf · hits/misses" value={confDeltaText} />
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
  const confMin = value.confidence_min ?? 1;

  // Match /signals's normalization — `undefined` is the "off" sentinel
  // (apiGet drops undefined values from the query string).
  const setAsset = (next: AssetSymbol | 'ALL') =>
    onChange({ ...value, asset: next === 'ALL' ? undefined : next });

  const setType = (next: SignalType | 'ALL') =>
    onChange({ ...value, signal_type: next === 'ALL' ? undefined : next });

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

  // Returns over the window — 72h close vs the canonical entry baseline.
  // Use `entry_price` (AI-suggested) when set, else `price_at_signal` —
  // SAME fallback as the backend evaluator's `entry_for_metrics` (see
  // `pipeline/track_record.py:_evaluate_one`) AND as `OutcomeCard`'s
  // `entryBaseline`. Diverging would mean this list cell shows a
  // different number than the detail page's `+72h` row for the same
  // outcome AND would contradict the verdict's hit_target math when
  // entry_price ≠ price_at_signal.
  const baseline = item.entry_price ?? item.price_at_signal;
  const pctReturn =
    item.price_after_72h !== null && baseline > 0
      ? ((item.price_after_72h - baseline) / baseline) * 100
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
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-20 w-full" />
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
