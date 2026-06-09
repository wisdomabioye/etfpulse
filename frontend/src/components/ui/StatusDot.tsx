type DotColor = 'accent' | 'pos' | 'neg' | 'warn' | 'info' | 'muted';

interface StatusDotProps {
  color?: DotColor;
  /** Adds an 8px glow using the dot color — used on hero/status indicators. */
  glow?: boolean;
  /** Hollow ring instead of filled dot — used for "pending" / inactive states. */
  hollow?: boolean;
  size?: 'sm' | 'md';
  className?: string;
}

// Reskinned to the new design tokens (R1): pos→win, neg→loss, muted→t4.
const COLOR_VAR: Record<DotColor, string> = {
  accent: 'var(--acc)',
  pos: 'var(--win)',
  neg: 'var(--loss)',
  warn: 'var(--warn)',
  info: 'var(--info)',
  muted: 'var(--t4)',
};

/**
 * Small colored circle used as a status/state indicator.
 * Hollow variant is used for "pending" readouts (e.g. HeroHitRatePanel's
 * ○ pending state before Stage 8 outcomes exist).
 */
export function StatusDot({
  color = 'pos',
  glow = false,
  hollow = false,
  size = 'sm',
  className = '',
}: StatusDotProps) {
  const bg = COLOR_VAR[color];
  const sizeClass = size === 'md' ? 'w-2 h-2' : 'w-1.5 h-1.5';

  return (
    <span
      className={`inline-block rounded-full ${sizeClass} ${className}`}
      style={{
        background: hollow ? 'transparent' : bg,
        border: hollow ? `1px solid ${bg}` : undefined,
        boxShadow: glow && !hollow ? `0 0 8px ${bg}` : undefined,
      }}
      aria-hidden
    />
  );
}
