import type { AssetSymbol, SignalFilters, SignalType, SortOrder } from '../../api/types';
import { ASSETS, DETECTORS, SIGNAL_TYPES } from '../../lib/constants';
import { FilterPill } from '../ui';

interface SignalFilterBarProps {
  value: SignalFilters;
  onChange: (next: SignalFilters) => void;
}

const SELECT_CLS =
  'font-mono text-[11px] bg-bg-3 text-t1 border border-line-3 rounded-sm px-2 py-1.5 focus-visible:outline-none';
const LABEL_CLS = 'font-mono text-[10px] text-t4 tracking-[0.1em] uppercase';

/**
 * Signals feed filter bar — reskinned (R6) to the prototype: asset pills, a
 * type select, a min-confidence range, an include-expired toggle, and a sort
 * select. Stateless/controlled — the page owns `SignalFilters`; this maps
 * each control to a partial update (preserving `limit`). "All" / floor values
 * clear the corresponding filter key rather than sending a no-op constraint.
 */
export function SignalFilterBar({ value, onChange }: SignalFilterBarProps) {
  const patch = (p: Partial<SignalFilters>) => onChange({ ...value, ...p });
  const minConf = value.confidence_min ?? 1;

  return (
    <div className="sticky top-[81px] z-30 border-b border-line-2 bg-bg-1/[0.92] backdrop-blur-md">
      <div className="max-w-[1200px] mx-auto px-6 py-3 flex gap-4 items-center flex-wrap">
        <div className="flex gap-1.5 items-center">
          <span className={LABEL_CLS}>asset</span>
          <FilterPill active={!value.asset} onClick={() => patch({ asset: undefined })}>
            All
          </FilterPill>
          {ASSETS.map((a) => (
            <FilterPill key={a} active={value.asset === a} onClick={() => patch({ asset: a as AssetSymbol })}>
              {a}
            </FilterPill>
          ))}
        </div>

        <span className="w-px h-[18px] bg-line-2" aria-hidden />

        <div className="flex gap-2 items-center">
          <span className={LABEL_CLS}>type</span>
          <select
            className={SELECT_CLS}
            aria-label="Signal type filter"
            value={value.signal_type ?? 'All'}
            onChange={(e) =>
              patch({ signal_type: e.target.value === 'All' ? undefined : (e.target.value as SignalType) })
            }
          >
            <option value="All">All types</option>
            {SIGNAL_TYPES.map((t) => (
              <option key={t} value={t}>
                {DETECTORS[t].label}
              </option>
            ))}
          </select>
        </div>

        <span className="w-px h-[18px] bg-line-2" aria-hidden />

        <div className="flex gap-2.5 items-center">
          <span className={LABEL_CLS}>min conf</span>
          <input
            type="range"
            min={1}
            max={10}
            value={minConf}
            aria-label="Minimum confidence"
            onChange={(e) => {
              const v = Number(e.target.value);
              patch({ confidence_min: v <= 1 ? undefined : v });
            }}
            className="w-[90px] accent-[var(--acc)]"
          />
          <span className="font-mono tabular-nums text-[11px] text-acc-hi w-9">{minConf}/10</span>
        </div>

        <span className="w-px h-[18px] bg-line-2" aria-hidden />

        <label className="flex gap-[7px] items-center cursor-pointer">
          <input
            type="checkbox"
            checked={value.include_expired ?? false}
            onChange={(e) => patch({ include_expired: e.target.checked || undefined })}
            className="accent-[var(--acc)]"
          />
          <span className="font-mono text-[11px] text-t2">incl. expired</span>
        </label>

        <span className="flex-1" />

        <div className="flex gap-2 items-center">
          <span className={LABEL_CLS}>sort</span>
          <select
            className={SELECT_CLS}
            aria-label="Sort order"
            value={value.sort ?? 'newest'}
            onChange={(e) => patch({ sort: e.target.value as SortOrder })}
          >
            <option value="newest">Newest</option>
            <option value="oldest">Oldest</option>
          </select>
        </div>
      </div>
    </div>
  );
}
