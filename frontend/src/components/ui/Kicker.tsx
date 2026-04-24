import type { ReactNode } from 'react';
import { StatusDot } from './StatusDot';

interface KickerProps {
  children: ReactNode;
  /** Optional leading dot. Defaults to glowing accent when enabled. */
  dot?: boolean;
  dotColor?: 'accent' | 'pos' | 'neg' | 'warn' | 'info' | 'muted';
  dotGlow?: boolean;
  className?: string;
}

/**
 * Mono uppercase eyebrow label above section headings. Matches the mock's
 * hero kicker ("SIGNALS INTELLIGENCE · BTC & ETH ETFS") and reused on
 * every page header for visual consistency.
 */
export function Kicker({
  children,
  dot = false,
  dotColor = 'accent',
  dotGlow = true,
  className = '',
}: KickerProps) {
  return (
    <div
      className={`flex items-center gap-2.5 font-mono text-[10px] tracking-[0.12em] uppercase text-text-3 ${className}`}
    >
      {dot && <StatusDot color={dotColor} glow={dotGlow} />}
      <span>{children}</span>
    </div>
  );
}
