import { useId } from 'react';

interface SparklineProps {
  /** Series values (left → right). */
  data: number[];
  width?: number;
  height?: number;
  /** Stroke + fill color (a CSS color or `cssVar(token)`). */
  color?: string;
  /** Draw the area gradient under the line. */
  fill?: boolean;
  strokeW?: number;
}

/**
 * Flow time-series sparkline — ported from the prototype. The gradient id
 * uses React's `useId()` (the prototype used `Math.random()`, which is banned
 * and breaks SSR/stability). `aria-hidden` — the number beside it carries the
 * meaning.
 */
export function Sparkline({
  data,
  width = 120,
  height = 32,
  color = 'var(--acc)',
  fill = true,
  strokeW = 1.5,
}: SparklineProps) {
  const gradId = useId();
  if (data.length === 0) return null;

  const min = Math.min(...data);
  const max = Math.max(...data);
  const rng = max - min || 1;
  const denom = data.length - 1 || 1;
  const pts = data.map(
    (d, i) => [(i / denom) * width, height - ((d - min) / rng) * (height - 4) - 2] as const,
  );
  const path = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const area = `${path} L ${width} ${height} L 0 ${height} Z`;

  return (
    <svg
      width={width}
      height={height}
      style={{ display: 'block', overflow: 'visible' }}
      aria-hidden="true"
    >
      {fill && (
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.22" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
      )}
      {fill && <path d={area} fill={`url(#${gradId})`} />}
      <path
        d={path}
        fill="none"
        stroke={color}
        strokeWidth={strokeW}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
