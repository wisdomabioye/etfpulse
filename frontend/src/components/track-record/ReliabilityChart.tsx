import { useState, type ReactNode } from 'react';
import type { CalibrationBucket, CalibrationResponse, HorizonLabel } from '../../api/types';
import { Kicker, Skeleton } from '../ui';

/**
 * PR I.1 — empirical reliability curve.
 *
 * Plots actual hit rate against stated AI confidence so the user can tell
 * whether "confidence 8" really means "80% hits." A perfectly-calibrated
 * model traces the y=x diagonal; one that overclaims sits below it; one
 * that underclaims sits above.
 *
 * The backend returns the full grid (every bucket × horizon combination),
 * so we render one horizon at a time with a tab strip — comparing buckets
 * apples-to-apples within a fixed window. Aggregating across horizons
 * would hide the (very common) case where swing is well-calibrated but
 * legacy/position cohorts have different curves.
 *
 * Empty cell handling:
 *   - n=0          → tile shows "—" + caption "no samples"
 *   - 0 < n < min  → tile shows "n samples · pending" with no rate bar
 *   - n >= min     → bar + Wilson CI whisker rendered, "p% (n)" labelled
 *
 * All numbers come from the backend as 0..1 fractions; we *100 on render.
 */

interface ReliabilityChartProps {
  data: CalibrationResponse;
  className?: string;
}

const HORIZON_ORDER: readonly HorizonLabel[] = [
  'scalp',
  'swing',
  'position',
  'legacy',
] as const;

const HORIZON_LABEL: Record<HorizonLabel, string> = {
  scalp: 'Scalp',
  swing: 'Swing',
  position: 'Position',
  legacy: 'Legacy',
};

export function ReliabilityChart({ data, className }: ReliabilityChartProps) {
  // Pre-pick swing — the bucket most likely to have data given current
  // detector mix + the kline gap that suppresses scalp scoring (#62).
  const [selectedHorizon, setSelectedHorizon] = useState<HorizonLabel>('swing');

  // Filter to selected horizon. Backend always emits the full grid, so
  // this slice is guaranteed to have exactly N buckets (5 by default).
  const horizonBuckets = data.buckets
    .filter((b) => b.horizon === selectedHorizon)
    .sort((a, b) => a.bucket_floor - b.bucket_floor);

  // Compute counts per horizon for the tab badge — quick visual cue that
  // shows the user where the data lives without forcing them to click.
  const countsByHorizon = (h: HorizonLabel): number =>
    data.buckets.filter((b) => b.horizon === h).reduce((n, b) => n + b.n_samples, 0);

  return (
    <section className={`${className ?? ''}`.trim()}>
      <Kicker dot={false}>
        Calibration · {data.ai_prompt_version} · last {data.lookback_days}d
      </Kicker>
      <h2
        className="text-[20px] font-semibold text-text-1 mt-1.5"
        style={{ letterSpacing: '-0.01em' }}
      >
        Are confidence scores honest?
      </h2>
      <p className="mt-1 text-[13px] text-text-3 max-w-[640px]">
        Empirical hit rate per confidence bucket. A perfectly-calibrated
        model traces the dashed diagonal. Bars below it overclaim; bars
        above it underclaim. Cells need at least {data.min_samples} samples
        before a rate is shown.
      </p>

      {/* Horizon selector — pill buttons matching the existing filter row
          style on /signals. Disabled-looking when a horizon has zero
          samples (the count badge reads "0") but still clickable so the
          user can confirm the bucket is empty rather than hidden. */}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        {HORIZON_ORDER.map((h) => {
          const total = countsByHorizon(h);
          const active = h === selectedHorizon;
          return (
            <button
              key={h}
              type="button"
              onClick={() => setSelectedHorizon(h)}
              className={
                'inline-flex items-center gap-2 px-3 py-1.5 rounded-md ' +
                'font-mono text-[11px] uppercase tracking-[0.08em] transition-colors ' +
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ' +
                (active
                  ? 'bg-accent/15 text-accent border border-accent/40'
                  : 'bg-bg-2 text-text-2 border border-border-2 hover:bg-bg-3')
              }
              aria-pressed={active}
              aria-label={`Show ${HORIZON_LABEL[h]} calibration (${total} samples)`}
            >
              <span>{HORIZON_LABEL[h]}</span>
              <span className={active ? 'text-accent' : 'text-text-4'}>
                {total}
              </span>
            </button>
          );
        })}
      </div>

      <ChartGrid
        buckets={horizonBuckets}
        minSamples={data.min_samples}
        className="mt-4"
      />
    </section>
  );
}

/* -------------------------------------------------------------------------
 * ChartGrid — the actual SVG bars + diagonal reference + per-bucket
 * captions. Kept in this file (not exported) because there's exactly one
 * caller and splitting would obscure the data flow.
 * ------------------------------------------------------------------------- */

const CHART_WIDTH = 640; // Internal viewport; CSS scales to container.
const CHART_HEIGHT = 280;
const PADDING = { top: 16, right: 20, bottom: 36, left: 40 };
const PLOT_WIDTH = CHART_WIDTH - PADDING.left - PADDING.right;
const PLOT_HEIGHT = CHART_HEIGHT - PADDING.top - PADDING.bottom;

function ChartGrid({
  buckets,
  minSamples,
  className,
}: {
  buckets: CalibrationBucket[];
  minSamples: number;
  className?: string;
}) {
  if (buckets.length === 0) {
    // Defensive — backend grid always has the bucket count, so this is
    // only ever true if the consumer passes pre-filtered data.
    return (
      <div className={`text-text-3 text-sm ${className ?? ''}`.trim()}>
        No calibration data.
      </div>
    );
  }

  // Bar layout — equal-width slots per bucket, with a small gap.
  const slotWidth = PLOT_WIDTH / buckets.length;
  const barWidth = slotWidth * 0.55;
  const barInset = (slotWidth - barWidth) / 2;

  // Y axis: 0..100%. 5 gridlines at 0/25/50/75/100 — same as the
  // analytics page's histograms.
  const yTicks = [0, 0.25, 0.5, 0.75, 1.0];
  const yToPixel = (y: number) => PADDING.top + (1 - y) * PLOT_HEIGHT;

  // X-axis position for the centre of a given bucket — used by both
  // the bar render and the diagonal-reference dotted line that maps the
  // bucket midpoint confidence to its corresponding y=x position.
  const bucketCentreX = (idx: number) =>
    PADDING.left + idx * slotWidth + slotWidth / 2;

  return (
    <div
      className={`rounded-md border border-border-2 bg-bg-2 px-4 py-4 ${className ?? ''}`.trim()}
    >
      <svg
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-label="Reliability curve: bucketed hit rate vs confidence"
        className="block w-full h-auto"
      >
        {/* Y-axis gridlines + labels */}
        {yTicks.map((t) => (
          <g key={t}>
            <line
              x1={PADDING.left}
              x2={CHART_WIDTH - PADDING.right}
              y1={yToPixel(t)}
              y2={yToPixel(t)}
              stroke="currentColor"
              strokeOpacity={0.08}
              strokeWidth={1}
              className="text-text-3"
            />
            <text
              x={PADDING.left - 8}
              y={yToPixel(t) + 3}
              textAnchor="end"
              className="fill-text-3 font-mono text-[9px]"
            >
              {Math.round(t * 100)}%
            </text>
          </g>
        ))}

        {/* y=x diagonal — perfect-calibration reference. Maps each
            bucket's MIDPOINT confidence (0.05..0.95) to its corresponding
            hit-rate position. Dashed to read as "reference, not data". */}
        <DiagonalReference
          buckets={buckets}
          bucketCentreX={bucketCentreX}
          yToPixel={yToPixel}
        />

        {/* Bars + CI whiskers per bucket */}
        {buckets.map((bucket, idx) => {
          const x0 = PADDING.left + idx * slotWidth + barInset;
          const centreX = bucketCentreX(idx);
          const baseY = yToPixel(0);
          const labelLine1 = `${bucket.bucket_floor}-${bucket.bucket_ceiling}`;

          // Three rendering states keyed on data availability:
          //   - hit_rate !== null: full bar + CI whiskers + "p% (n)" label
          //   - n > 0 && hit_rate === null: empty slot + "n · pending"
          //   - n === 0: empty slot + "—"
          const hasRate = bucket.hit_rate !== null;
          const hasAnyData = bucket.n_samples > 0;

          let barEl: ReactNode = null;
          let whiskerEl: ReactNode = null;
          let captionLine2: string;

          if (hasRate && bucket.hit_rate !== null) {
            const barTop = yToPixel(bucket.hit_rate);
            // Bars colored by direction of miscalibration vs the bucket
            // midpoint (the diagonal reference). Greater than expected →
            // pos (model underclaimed, conservative); less than expected
            // → neg (model overclaimed, dangerous overconfidence).
            const expected = (bucket.bucket_floor + bucket.bucket_ceiling) / 2 / 10;
            const calibClass =
              bucket.hit_rate >= expected ? 'fill-pos/70' : 'fill-neg/70';
            barEl = (
              <rect
                x={x0}
                y={barTop}
                width={barWidth}
                height={baseY - barTop}
                rx={2}
                className={calibClass}
              />
            );
            // CI whisker — vertical line capped on both ends. Centred on
            // the bar's centre, spanning ci_low..ci_high.
            if (bucket.ci_low !== null && bucket.ci_high !== null) {
              const yLow = yToPixel(bucket.ci_low);
              const yHigh = yToPixel(bucket.ci_high);
              const capHalf = barWidth * 0.18;
              whiskerEl = (
                <g
                  stroke="currentColor"
                  strokeWidth={1.5}
                  strokeOpacity={0.7}
                  className="text-text-1"
                  fill="none"
                >
                  <line x1={centreX} x2={centreX} y1={yHigh} y2={yLow} />
                  <line
                    x1={centreX - capHalf}
                    x2={centreX + capHalf}
                    y1={yHigh}
                    y2={yHigh}
                  />
                  <line
                    x1={centreX - capHalf}
                    x2={centreX + capHalf}
                    y1={yLow}
                    y2={yLow}
                  />
                </g>
              );
            }
            captionLine2 = `${Math.round(bucket.hit_rate * 100)}% · n=${bucket.n_samples}`;
          } else if (hasAnyData) {
            // Below min_samples — show counts so the user sees the bucket
            // exists but the rate isn't trustworthy yet.
            captionLine2 = `n=${bucket.n_samples} · need ${minSamples}`;
          } else {
            captionLine2 = '—';
          }

          return (
            <g key={`${bucket.bucket_floor}-${bucket.bucket_ceiling}`}>
              {barEl}
              {whiskerEl}
              <text
                x={centreX}
                y={CHART_HEIGHT - PADDING.bottom + 14}
                textAnchor="middle"
                className="fill-text-2 font-mono text-[10px]"
              >
                {labelLine1}
              </text>
              <text
                x={centreX}
                y={CHART_HEIGHT - PADDING.bottom + 26}
                textAnchor="middle"
                className={`font-mono text-[9px] ${
                  hasRate ? 'fill-text-3' : 'fill-text-4'
                }`}
              >
                {captionLine2}
              </text>
            </g>
          );
        })}

        {/* Axis labels */}
        <text
          x={PADDING.left + PLOT_WIDTH / 2}
          y={CHART_HEIGHT - 4}
          textAnchor="middle"
          className="fill-text-3 font-mono text-[10px] uppercase tracking-[0.1em]"
        >
          Confidence bucket
        </text>
        <text
          x={-(PADDING.top + PLOT_HEIGHT / 2)}
          y={12}
          textAnchor="middle"
          transform="rotate(-90)"
          className="fill-text-3 font-mono text-[10px] uppercase tracking-[0.1em]"
        >
          Hit rate
        </text>
      </svg>

      {/* Legend strip below the chart. Tight inline keys so the chart
          itself doesn't need an in-svg legend block (which always renders
          smaller than ideal). */}
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mono text-[10px] text-text-3 uppercase tracking-[0.08em]">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block w-3 h-2 rounded-sm bg-pos/70" />
          underclaimed
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block w-3 h-2 rounded-sm bg-neg/70" />
          overclaimed
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block w-3 h-[1.5px] bg-text-1/70" />
          95% ci
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block w-3 border-t border-dashed border-text-3" />
          perfect (y=x)
        </span>
      </div>
    </div>
  );
}

function DiagonalReference({
  buckets,
  bucketCentreX,
  yToPixel,
}: {
  buckets: CalibrationBucket[];
  bucketCentreX: (idx: number) => number;
  yToPixel: (y: number) => number;
}) {
  // For each bucket midpoint we plot the y=x reference point, then
  // connect them with a dashed segment. This produces a piecewise-linear
  // diagonal that lines up with the bar centres exactly — using the raw
  // axis diagonal would visually misalign at the bucket boundaries.
  const points = buckets.map((b, idx) => {
    const midConfidence = (b.bucket_floor + b.bucket_ceiling) / 2 / 10;
    return { x: bucketCentreX(idx), y: yToPixel(midConfidence) };
  });
  const path = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`)
    .join(' ');
  return (
    <g aria-hidden>
      <path
        d={path}
        fill="none"
        stroke="currentColor"
        strokeWidth={1}
        strokeOpacity={0.45}
        strokeDasharray="4 3"
        className="text-text-3"
      />
      {points.map((p, idx) => (
        <circle
          key={idx}
          cx={p.x}
          cy={p.y}
          r={2.5}
          fill="currentColor"
          fillOpacity={0.5}
          className="text-text-3"
        />
      ))}
    </g>
  );
}

/** Skeleton placeholder while the calibration query is loading. Matches the
 *  real component's outer shape so the layout doesn't shift on hydrate. */
export function ReliabilityChartSkeleton({ className }: { className?: string }) {
  return (
    <section className={`${className ?? ''}`.trim()}>
      <Skeleton className="h-5 w-48 mb-2" />
      <Skeleton className="h-4 w-80 mb-3" />
      <div className="mt-3 flex gap-2">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-7 w-20" />
        ))}
      </div>
      <Skeleton className="mt-4 h-[280px] w-full" />
    </section>
  );
}
