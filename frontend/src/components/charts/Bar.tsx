interface BarProps {
  value: number;
  max?: number;
  /** Fill color (CSS color or `cssVar(token)`). */
  color?: string;
  height?: number;
  width?: number | string;
  /** Track (background) color. */
  track?: string;
}

/**
 * Horizontal proportion bar — ported from the prototype. The fill sweeps in
 * via the `sweep` keyframe (reduced-motion-gated in index.css). Clamped to
 * 0–100%.
 */
export function Bar({
  value,
  max = 1,
  color = 'var(--acc)',
  height = 7,
  width = '100%',
  track = 'var(--bg-3)',
}: BarProps) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div style={{ width, height, background: track, borderRadius: 99, overflow: 'hidden' }}>
      <div
        style={{
          width: `${pct}%`,
          height: '100%',
          background: color,
          borderRadius: 99,
          transformOrigin: 'left',
          animation: 'sweep var(--dur-2) var(--ease)',
        }}
      />
    </div>
  );
}
