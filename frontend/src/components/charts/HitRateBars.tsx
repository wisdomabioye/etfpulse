import { HORIZONS } from '../../lib/constants';
import { formatPct } from '../../lib/format';
import { Bar } from './Bar';
import type { HitRateRow } from './types';

interface HitRateBarsProps {
  data: HitRateRow[];
}

/**
 * Hit-rate by horizon — one labeled proportion bar per horizon, colored by a
 * quality ramp (≥70% win · ≥55% conf-mid · else warn). Ported from the
 * prototype; the prototype's inline `#7dd3a8` is now the `--conf-mid` token.
 */
export function HitRateBars({ data }: HitRateBarsProps) {
  return (
    <div className="flex flex-col gap-3.5">
      {data.map((r) => {
        const color =
          r.hit >= 0.7 ? 'var(--win)' : r.hit >= 0.55 ? 'var(--conf-mid)' : 'var(--warn)';
        const hz = HORIZONS.find((h) => h.key === r.horizon);
        return (
          <div key={r.horizon}>
            <div className="flex justify-between items-baseline mb-1.5">
              <span className="text-[13px] capitalize">
                {hz?.label}{' '}
                <span className="font-mono text-t4 text-[10px]">{hz?.window}</span>
              </span>
              <span className="font-mono tabular-nums text-[13px] font-semibold" style={{ color }}>
                {formatPct(r.hit)}{' '}
                {r.n !== undefined && (
                  <span className="text-t4 font-normal text-[10px]">n={r.n}</span>
                )}
              </span>
            </div>
            <Bar value={r.hit} color={color} height={9} />
          </div>
        );
      })}
    </div>
  );
}
