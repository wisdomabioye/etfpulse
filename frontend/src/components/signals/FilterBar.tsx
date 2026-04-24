import type { AssetSymbol, SignalFilters, SignalType, SortOrder } from '../../api/types';
import { formatSignalType } from '../../lib/format';
import { FilterPill } from '../ui';

interface FilterBarProps {
  value: SignalFilters;
  onChange: (next: SignalFilters) => void;
}

const SIGNAL_TYPES: SignalType[] = [
  'flow_anomaly',
  'magnitude',
  'acceleration',
  'divergence',
  'regime_shift',
];

const LABEL =
  'font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] select-none';

const SELECT_CLASS =
  'bg-bg-3 text-text-1 border border-border-3 rounded-[5px] px-[10px] py-[7px] text-[12px] font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 disabled:cursor-not-allowed';

// Hidden below sm — on wrapped/mobile layout the rule sits at end-of-line
// and reads as a stranded artifact rather than a divider.
const VERTICAL_RULE = <div className="hidden sm:block w-px h-5 bg-border-2" aria-hidden />;

/**
 * Sticky filter bar above the signals feed. Four groups separated by
 * vertical hairline rules: asset · type · confidence · sort.
 *
 * Backend reality check:
 *  - `confidence` slider is SINGLE-handle (confidence_min only) — the
 *    wireframe's dual-handle would imply a confidence_max the API doesn't
 *    have. Honest UI > decorative handles. Followup: open_issues.md #45.
 *
 * Sticky lives at top-0 inside the scroll root; the app's TopNav is not
 * itself sticky, so the filter bar takes over as the top-stuck element
 * once the page title scrolls away.
 */
export function FilterBar({ value, onChange }: FilterBarProps) {
  const asset = value.asset ?? 'ALL';
  const type = value.signal_type ?? 'ALL';
  const confMin = value.confidence_min ?? 1;
  const sort: SortOrder = value.sort ?? 'newest';

  // apiGet drops undefined values (see api/client.ts#buildUrl), so setting
  // a filter field to undefined is the normalized "off" state. TanStack's
  // query-key comparison treats {a: undefined} and {} as equivalent, so no
  // spurious refetches from the key differing.
  const setAsset = (next: AssetSymbol | 'ALL') =>
    onChange({ ...value, asset: next === 'ALL' ? undefined : next });

  const setType = (next: SignalType | 'ALL') =>
    onChange({ ...value, signal_type: next === 'ALL' ? undefined : next });

  const setConfMin = (next: number) =>
    onChange({ ...value, confidence_min: next <= 1 ? undefined : next });

  const setSort = (next: SortOrder) =>
    onChange({ ...value, sort: next === 'newest' ? undefined : next });

  return (
    <div
      className="sticky top-0 z-10 bg-bg-1 border-b border-border-2 px-6 sm:px-8 py-3.5 flex items-center gap-4 flex-wrap"
      role="toolbar"
      aria-label="Filter signals"
    >
      {/* Asset pills */}
      <div className="flex items-center gap-4">
        <span className={LABEL}>Asset</span>
        <div className="flex gap-1">
          <FilterPill active={asset === 'ALL'} onClick={() => setAsset('ALL')}>
            All
          </FilterPill>
          <FilterPill active={asset === 'BTC'} onClick={() => setAsset('BTC')}>
            BTC
          </FilterPill>
          <FilterPill active={asset === 'ETH'} onClick={() => setAsset('ETH')}>
            ETH
          </FilterPill>
        </div>
      </div>

      {VERTICAL_RULE}

      {/* Type select */}
      <div className="flex items-center gap-3">
        <span className={LABEL}>Type</span>
        <select
          className={SELECT_CLASS}
          style={{ minWidth: 150 }}
          value={type}
          onChange={(e) => setType(e.target.value as SignalType | 'ALL')}
          aria-label="Signal type"
        >
          <option value="ALL">All types</option>
          {SIGNAL_TYPES.map((t) => (
            <option key={t} value={t}>
              {formatSignalType(t)}
            </option>
          ))}
        </select>
      </div>

      {VERTICAL_RULE}

      {/* Confidence — min-only slider (backend has confidence_min only) */}
      <div className="flex items-center gap-2.5">
        <span className={LABEL}>Min confidence</span>
        <input
          type="range"
          min={1}
          max={10}
          step={1}
          value={confMin}
          onChange={(e) => setConfMin(Number(e.target.value))}
          aria-label="Minimum confidence"
          className="confidence-slider w-40 accent-accent"
        />
        <span className="font-mono text-[11px] text-text-2 tabular-nums w-[2ch] text-right">
          {confMin}
        </span>
      </div>

      <div className="flex-1" />

      {/* Sort */}
      <div className="flex items-center gap-2.5">
        <span className={LABEL}>Sort</span>
        <select
          className={SELECT_CLASS}
          value={sort}
          onChange={(e) => setSort(e.target.value as SortOrder)}
          aria-label="Sort order"
        >
          <option value="newest">Newest</option>
          <option value="oldest">Oldest</option>
        </select>
      </div>
    </div>
  );
}
