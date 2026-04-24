import type { ReactNode } from 'react';

interface TrendIndicator {
  dir: 'up' | 'down' | 'flat';
  value: string;
}

interface StatTileProps {
  label: string;
  /** Display value — already formatted (e.g. "156", "+2.2%", "6.4"). */
  value: ReactNode;
  trend?: TrendIndicator;
  size?: 'sm' | 'md' | 'lg';
  /** Remove border + bg for inline contexts. */
  flush?: boolean;
}

/**
 * Big number + label + optional trend indicator. Backs the home page
 * dashboard tiles.
 *
 * Matches mock's MetricTile — bg-2 card, tabular-nums on the value,
 * uppercase mono label, optional ↑/↓/→ trend in pos/neg/muted.
 */
export function StatTile({
  label,
  value,
  trend,
  size = 'md',
  flush = false,
}: StatTileProps) {
  const valueSize =
    size === 'lg' ? 'text-[36px]' : size === 'sm' ? 'text-[22px]' : 'text-[28px]';
  const padding =
    size === 'lg' ? 'px-6 py-6' : size === 'sm' ? 'px-4 py-[14px]' : 'px-5 py-[18px]';
  const borderClass = flush ? '' : 'border border-border-2';
  const bgClass = flush ? '' : 'bg-bg-2';

  const trendColor =
    trend?.dir === 'up'
      ? 'text-pos'
      : trend?.dir === 'down'
        ? 'text-neg'
        : 'text-text-3';
  const trendArrow = trend?.dir === 'up' ? '↑' : trend?.dir === 'down' ? '↓' : '→';

  return (
    <div className={`rounded-md ${borderClass} ${bgClass} ${padding}`}>
      <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mb-2">
        {label}
      </div>
      <div
        className={`${valueSize} font-semibold text-text-1 tabular-nums leading-none`}
        style={{ letterSpacing: '-0.02em' }}
      >
        {value}
      </div>
      {trend && (
        <div className={`mt-2 font-mono text-[11px] ${trendColor}`}>
          {trendArrow} {trend.value}
        </div>
      )}
    </div>
  );
}
