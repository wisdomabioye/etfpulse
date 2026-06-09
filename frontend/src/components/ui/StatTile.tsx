import type { ReactNode } from 'react';

import { useLiveNumber } from '../../hooks/useLiveNumber';

interface TrendIndicator {
  dir: 'up' | 'down' | 'flat';
  value: string;
}

interface StatTileProps {
  label: string;
  /** Display value — already formatted (e.g. "156", "+2.2%", "6.4"), or a
   *  raw number when `live` is set (then this hook eases toward it). */
  value: ReactNode;
  trend?: TrendIndicator;
  /** Mono caption beside the trend (e.g. a sample count "n=204"). */
  sub?: ReactNode;
  size?: 'sm' | 'md' | 'lg';
  /** Color the value with the amber accent (hero emphasis). */
  accent?: boolean;
  /** Ease the value toward its target (only when `value` is a number). */
  live?: boolean;
  /** Remove border + bg for inline contexts. */
  flush?: boolean;
}

/**
 * Big number + label + optional trend — reskinned (R1) to the prototype's
 * `StatTile`. New props: `accent` (amber value), `sub` (mono caption), and
 * `live` (eased count-up via `useLiveNumber`, motion-gated). Trend keeps the
 * ↑/↓/→ in win/loss/muted.
 */
export function StatTile({
  label,
  value,
  trend,
  sub,
  size = 'md',
  accent = false,
  live = false,
  flush = false,
}: StatTileProps) {
  // `useLiveNumber` must be called unconditionally; it no-ops when not live.
  const isLiveNum = live && typeof value === 'number';
  const eased = useLiveNumber(isLiveNum ? (value as number) : 0, { live: isLiveNum });
  const display: ReactNode = isLiveNum
    ? (value as number) >= 1000
      ? Math.round(eased).toLocaleString('en-US')
      : eased.toFixed((value as number) < 10 ? 1 : 0)
    : value;

  const valueSize =
    size === 'lg' ? 'text-[38px]' : size === 'sm' ? 'text-[22px]' : 'text-[26px]';
  const padding =
    size === 'lg' ? 'px-6 py-6' : size === 'sm' ? 'px-4 py-[14px]' : 'px-5 py-[18px]';
  const shell = flush ? '' : 'border border-line-2 bg-bg-2';

  const trendColor =
    trend?.dir === 'up' ? 'text-win' : trend?.dir === 'down' ? 'text-loss' : 'text-t3';
  const trendArrow = trend?.dir === 'up' ? '↑' : trend?.dir === 'down' ? '↓' : '→';

  return (
    <div className={`rounded-md ${shell} ${padding}`.trim()}>
      <div className="font-mono text-[10px] text-t3 uppercase tracking-[0.1em] mb-2.5">
        {label}
      </div>
      <div
        className={`${valueSize} font-semibold tabular-nums leading-none ${accent ? 'text-acc-hi' : 'text-t1'}`}
        style={{ letterSpacing: '-0.02em' }}
      >
        {display}
      </div>
      {(trend || sub) && (
        <div className="mt-[9px] flex items-center gap-2">
          {trend && (
            <span className={`font-mono tabular-nums text-[11px] ${trendColor}`}>
              {trendArrow} {trend.value}
            </span>
          )}
          {sub && <span className="font-mono text-[10px] text-t4">{sub}</span>}
        </div>
      )}
    </div>
  );
}
