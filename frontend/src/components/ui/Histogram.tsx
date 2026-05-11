import type { HistogramBucket } from '../../api/types';

interface HistogramProps {
  buckets: HistogramBucket[];
  /** Total chart height in pixels (count labels + bars + x-axis labels).
   *  Default 160 — tall enough to read distribution shape without dominating
   *  the page. Width is responsive (100% of parent). */
  height?: number;
  /** ARIA label for the chart. Required because screen readers can't infer
   *  meaning from the bar visuals alone. */
  ariaLabel: string;
}

// Reserved vertical space (px) for the count labels above bars and the
// category labels below the x-axis. Subtracted from `height` to get the
// remaining bar area. Tuned for the 11px monospace text used below.
const _COUNT_LABEL_HEIGHT = 18;
const _AXIS_LABEL_HEIGHT = 22;

/**
 * Reusable bar chart for `HistogramBucket[]`. Used twice on the Analytics
 * page (MFE + MAE) — generic enough that any future bucket-count
 * distribution renders through the same component.
 *
 * Bar heights are normalized against the MAX count in the dataset
 * (tallest bar = full bar-area height). The shape is preserved regardless
 * of total — a chart with `[5, 1, 1]` and a chart with `[500, 100, 100]`
 * look identical. The page-level caption carries the absolute totals so
 * statistical weight is visible without misleadingly tiny bars.
 *
 * Why HTML+CSS instead of SVG (revised from the initial SVG cut):
 * making the SVG responsive required `preserveAspectRatio="none"`, which
 * stretches text horizontally — letters appeared squashed in height
 * relative to their width. Native DOM text + flexbox bars side-steps
 * the issue entirely: text picks up the page's font metrics directly,
 * bars scale via flex + pixel heights with no aspect-ratio gymnastics.
 *
 * Empty state (all counts === 0): renders the x-axis labels with no
 * visible bars. The chart structure stays present so cold-boot readers
 * still see what dimension the chart will show once data arrives.
 */
export function Histogram({ buckets, height = 160, ariaLabel }: HistogramProps) {
  // Defensive — never happens in practice (backend always returns 6
  // buckets), but a render call with `buckets={[]}` shouldn't NaN.
  if (buckets.length === 0) {
    return null;
  }

  const maxCount = Math.max(...buckets.map((b) => b.count));
  const barAreaHeight = height - _COUNT_LABEL_HEIGHT - _AXIS_LABEL_HEIGHT;

  return (
    <div role="img" aria-label={ariaLabel} style={{ height }}>
      {/* Bars row — flex column per bucket, bottom-aligned so bar grows
          upward from the baseline. Count label reserves its own space
          via `visibility: hidden` when count is 0, so columns stay
          visually aligned across the chart. */}
      <div
        className="flex items-end gap-1.5"
        style={{ height: _COUNT_LABEL_HEIGHT + barAreaHeight }}
      >
        {buckets.map((bucket) => {
          const barHeight =
            maxCount === 0 ? 0 : (bucket.count / maxCount) * barAreaHeight;
          return (
            <div
              key={bucket.label}
              className="flex-1 flex flex-col items-center justify-end"
            >
              <span
                className="text-[11px] font-mono text-text-2 mb-1 leading-none"
                style={{ visibility: bucket.count > 0 ? 'visible' : 'hidden' }}
              >
                {bucket.count}
              </span>
              {/* Bar — only rendered with `height > 0`. A bar with `count=0`
                  has `barHeight=0`; the div still renders but at zero
                  height, which is what we want (no visual artifact). */}
              <div
                className="w-full bg-accent rounded-t-sm"
                style={{ height: `${barHeight}px`, opacity: 0.85 }}
              />
            </div>
          );
        })}
      </div>
      {/* X-axis labels — separate flex row matching the bar gap so columns
          line up. Border-top on this row gives the chart its baseline rule. */}
      <div
        className="flex gap-1.5 border-t border-border-2 pt-1.5"
        style={{ height: _AXIS_LABEL_HEIGHT }}
      >
        {buckets.map((bucket) => (
          <div
            key={bucket.label}
            className="flex-1 text-center text-[11px] font-mono text-text-3 leading-none"
          >
            {bucket.label}
          </div>
        ))}
      </div>
    </div>
  );
}
