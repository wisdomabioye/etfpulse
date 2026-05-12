import type { AssetSymbol, SignalFilters, SignalType, SortOrder } from '../../api/types';
import { ASSETS, SIGNAL_TYPES } from '../../lib/constants';
import { formatSignalType } from '../../lib/format';
import { FilterPill } from '../ui';

interface FilterBarProps {
  value: SignalFilters;
  onChange: (next: SignalFilters) => void;
}

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
  const includeExpired = value.include_expired === true;

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

  const setIncludeExpired = (next: boolean) =>
    onChange({ ...value, include_expired: next ? true : undefined });

  return (
    <div className="sticky top-0 z-10 bg-bg-1 border-b border-border-2">
      <div
        className="px-6 sm:px-8 py-3.5 flex items-center gap-4 flex-wrap"
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
          {ASSETS.map((sym) => (
            <FilterPill
              key={sym}
              active={asset === sym}
              onClick={() => setAsset(sym)}
            >
              {sym}
            </FilterPill>
          ))}
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

      {VERTICAL_RULE}

      {/* Include expired — off by default per backend contract; alerts have a
          shelf life and the feed hides them after `expires_at` to avoid
          surfacing stale guidance. Toggling on shows the full history,
          including signals past their `expires_at`. */}
      <label className="flex items-center gap-2 cursor-pointer select-none">
        <input
          type="checkbox"
          className="accent-accent w-3.5 h-3.5 cursor-pointer"
          checked={includeExpired}
          onChange={(e) => setIncludeExpired(e.target.checked)}
          aria-label="Include expired signals"
        />
        <span className={LABEL}>Include expired</span>
      </label>
      </div>

      {/* Hint banner — only visible when the toggle is active. Tells the user
          they're seeing alerts past their shelf life so a stale recommendation
          isn't acted on as if it were live. */}
      {includeExpired ? (
        <div
          className="px-6 sm:px-8 py-2 border-t border-border-2 bg-bg-2 flex items-center gap-2"
          role="status"
        >
          <span
            className="inline-block w-1.5 h-1.5 rounded-full bg-warn"
            aria-hidden
          />
          <p className="font-mono text-[11px] text-text-2 leading-tight">
            Showing expired signals — these alerts are past their{' '}
            <code className="text-text-1">expires_at</code> and are kept here
            for review only. Don&apos;t act on them as live guidance.
          </p>
        </div>
      ) : null}
    </div>
  );
}
