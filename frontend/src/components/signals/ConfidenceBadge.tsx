import { confidenceColor } from '../../lib/format';

interface ConfidenceBadgeProps {
  value: number | null;
  size?: 'sm' | 'lg';
}

/**
 * Color-coded confidence readout. Renders nothing when `value` is null
 * (AI-failed signals — caller controls layout around the absent badge).
 *
 * Size:
 *   - sm (default): inline pill with dot + "7/10" mono readout
 *   - lg: vertical card with glowing dot + big value + "confidence" label
 *
 * Colors (via lib/format.ts#confidenceColor):
 *   ≥7 pos/green · 4-6 warn/amber · <4 neg/red
 *
 * Uses CSS `color-mix` for the 14% bg + 30% border tint — inline style so
 * the computed color token cascades at render time.
 */
export function ConfidenceBadge({ value, size = 'sm' }: ConfidenceBadgeProps) {
  if (value === null) return null;

  const color = confidenceColor(value);
  const bg = `color-mix(in oklab, ${color} 14%, transparent)`;
  const borderColor = `color-mix(in oklab, ${color} 30%, transparent)`;

  if (size === 'lg') {
    return (
      <div
        className="inline-flex flex-col items-start rounded-lg px-[14px] py-[10px]"
        style={{ background: bg, border: `1px solid ${borderColor}` }}
      >
        <div className="flex items-center gap-2">
          <span
            className="w-2 h-2 rounded-full"
            style={{ background: color, boxShadow: `0 0 8px ${color}` }}
          />
          <span className="font-mono text-[22px] font-semibold text-text-1 tabular-nums">
            {value}
            <span className="text-text-3">/10</span>
          </span>
        </div>
        <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mt-0.5">
          confidence
        </div>
      </div>
    );
  }

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-[5px] px-2 py-[3px] font-mono text-[11px] font-semibold text-text-1 tabular-nums"
      style={{ background: bg, border: `1px solid ${borderColor}` }}
    >
      <span
        className="w-1.5 h-1.5 rounded-full"
        style={{ background: color }}
      />
      {value}
      <span className="text-text-3 font-normal">/10</span>
    </span>
  );
}
