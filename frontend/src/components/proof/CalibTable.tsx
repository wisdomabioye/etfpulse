import type { CalibrationCell } from '../charts';
import { Bar } from '../charts';
import { BUCKET_IDEAL, CONF_BUCKETS } from '../../lib/constants';
import { cssVar } from '../../lib/colorMix';
import { formatPct } from '../../lib/format';

interface CalibTableProps {
  cells: CalibrationCell[];
}

/**
 * Per-bucket calibration detail table beside the curve — ported from the
 * prototype's `CalibTable`. Each sufficient bucket shows a hit-rate bar
 * (green when at/above the ideal midpoint, red below), its 95% CI, and n.
 * Insufficient buckets render an honest "— insufficient (n=…)".
 */
export function CalibTable({ cells }: CalibTableProps) {
  return (
    <div className="flex flex-col">
      <div className="grid gap-2 pb-2 border-b border-line-2" style={{ gridTemplateColumns: '52px 1fr 70px 44px' }}>
        {['Conf', 'Hit rate', '95% CI', 'n'].map((h, i) => (
          <span
            key={h}
            className={`font-mono text-[9px] text-t4 tracking-[0.08em] uppercase ${i > 1 ? 'text-right' : 'text-left'}`}
          >
            {h}
          </span>
        ))}
      </div>
      {cells.map((c, i) => (
        <div
          key={i}
          className="grid gap-2 items-center py-[9px] border-b border-line-1"
          style={{ gridTemplateColumns: '52px 1fr 70px 44px' }}
        >
          <span className="font-mono tabular-nums text-[12px]">{CONF_BUCKETS[i]}</span>
          {c.insufficient ? (
            <span className="font-mono text-[11px] text-t4" style={{ gridColumn: '2 / 5' }}>
              — insufficient (n={c.n})
            </span>
          ) : (
            <>
              <Bar
                value={c.hit}
                color={cssVar(c.hit >= BUCKET_IDEAL[i] ? '--win' : '--loss')}
                height={7}
              />
              <span className="font-mono tabular-nums text-[10.5px] text-t3 text-right">
                {formatPct(c.ci_low, 0)}–{formatPct(c.ci_high, 0)}
              </span>
              <span className="font-mono tabular-nums text-[11px] text-t4 text-right">{c.n}</span>
            </>
          )}
        </div>
      ))}
    </div>
  );
}
