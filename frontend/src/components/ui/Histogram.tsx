import type { HistogramBucket } from '../../api/types';

interface HistogramProps {
  buckets: HistogramBucket[];
  /** Chart height in pixels. Default 140 — tall enough to see distribution
   *  shape without dominating the page. Width is responsive (100% of parent). */
  height?: number;
  /** ARIA label for the chart. Required when a Histogram is the only
   *  visual conveying the data — screen readers otherwise see only bar
   *  rectangles with no context. */
  ariaLabel: string;
}

// SVG viewBox dimensions — actual rendered size is 100% width × `height`
// via the wrapper div. Using a fixed viewBox lets the SVG scale crisply
// without rewriting positions on resize.
const _VB_WIDTH = 600;
const _VB_HEIGHT = 200;
// Insets reserve room for the count labels (top) and bucket labels (bottom).
// Tuned by eye against a 600×200 viewBox; safe margin for typical fonts.
const _INSET_TOP = 22;
const _INSET_BOTTOM = 28;
const _BAR_GAP = 6;

/**
 * Reusable SVG bar chart for `HistogramBucket[]`. Used twice on the
 * Analytics page (MFE + MAE) — kept generic so any future bucket-count
 * distribution renders through the same component.
 *
 * Bar heights are normalized against the MAX count in the dataset
 * (tallest bar = full chart height). This means a chart with `[5, 1, 1]`
 * and a chart with `[500, 100, 100]` look identical in shape — the
 * caption above the chart carries the absolute totals so readers see
 * statistical weight without the bars being misleadingly tiny.
 *
 * Pure SVG — no Recharts, no Chart.js. Bundle stays small (Stage 06
 * "no chart library" trade-off documented in CLAUDE.md).
 *
 * Empty state (all counts === 0): renders the bucket labels along the
 * x-axis but no bars. The chart structure stays visible so cold-boot
 * readers see the dimension exists.
 */
export function Histogram({ buckets, height = 140, ariaLabel }: HistogramProps) {
  // Guard against zero-bucket input — pure defense; never happens in
  // practice (the backend always returns 6 buckets), but a render call
  // with `buckets={[]}` shouldn't NaN through dividing by zero.
  if (buckets.length === 0) {
    return null;
  }

  const maxCount = Math.max(...buckets.map((b) => b.count));
  const chartTop = _INSET_TOP;
  const chartBottom = _VB_HEIGHT - _INSET_BOTTOM;
  const chartHeight = chartBottom - chartTop;

  // Equal-width bars across the viewBox, with a small gap between them.
  // Math: total bar space = viewWidth, divided into N bars with (N-1)*GAP
  // of gap removed first, leaves bar width.
  const totalGap = _BAR_GAP * (buckets.length - 1);
  const barWidth = (_VB_WIDTH - totalGap) / buckets.length;

  return (
    <svg
      viewBox={`0 0 ${_VB_WIDTH} ${_VB_HEIGHT}`}
      width="100%"
      height={height}
      role="img"
      aria-label={ariaLabel}
      preserveAspectRatio="none"
    >
      {/* Baseline rule — subtle border so an all-zero chart still has
          a visible x-axis. */}
      <line
        x1={0}
        y1={chartBottom}
        x2={_VB_WIDTH}
        y2={chartBottom}
        stroke="var(--color-border-2)"
        strokeWidth={1}
      />
      {buckets.map((bucket, i) => {
        const x = i * (barWidth + _BAR_GAP);
        // Normalized bar height — 0 when count is 0 (no bar drawn), full
        // chart height when count === maxCount. `maxCount === 0` collapses
        // to a no-op via the multiplier check.
        const barHeight = maxCount === 0 ? 0 : (bucket.count / maxCount) * chartHeight;
        const barY = chartBottom - barHeight;
        // Label centering — same x for bar and the labels above/below it.
        const labelX = x + barWidth / 2;
        return (
          <g key={bucket.label}>
            {/* Bar — only rendered when there's something to show. A
                zero-height bar would render as a thin line, which reads
                as a non-empty bucket. */}
            {bucket.count > 0 && (
              <rect
                x={x}
                y={barY}
                width={barWidth}
                height={barHeight}
                fill="var(--color-accent)"
                opacity={0.85}
                rx={2}
              />
            )}
            {/* Count label above bar — only when there's a bar to label.
                Renders inside the top inset reserved at viewBox creation. */}
            {bucket.count > 0 && (
              <text
                x={labelX}
                y={barY - 6}
                textAnchor="middle"
                fontSize={11}
                fill="var(--color-text-2)"
                fontFamily="ui-monospace, SFMono-Regular, monospace"
              >
                {bucket.count}
              </text>
            )}
            {/* Bucket label below baseline — always rendered so the
                x-axis is readable even with all-zero data. */}
            <text
              x={labelX}
              y={chartBottom + 18}
              textAnchor="middle"
              fontSize={11}
              fill="var(--color-text-3)"
              fontFamily="ui-monospace, SFMono-Regular, monospace"
            >
              {bucket.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
