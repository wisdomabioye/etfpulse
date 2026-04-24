import type { ReactNode } from 'react';

type Tone = 'warn' | 'pos' | 'neg' | 'info';

interface CalloutProps {
  tone?: Tone;
  children: ReactNode;
  className?: string;
}

const TONE_VAR: Record<Tone, string> = {
  warn: 'var(--color-warn)',
  pos: 'var(--color-pos)',
  neg: 'var(--color-neg)',
  info: 'var(--color-info)',
};

/**
 * Left-border tinted card. Matches the mock's "Risks" list item: 2px
 * left-border in the tone color, 5% tint background, rounded only on the
 * right side so the border reads as a flag.
 */
export function Callout({ tone = 'warn', children, className = '' }: CalloutProps) {
  const color = TONE_VAR[tone];
  return (
    <div
      className={`px-4 py-2.5 text-[14px] leading-[1.55] text-text-2 ${className}`.trim()}
      style={{
        borderLeft: `2px solid ${color}`,
        background: `color-mix(in oklab, ${color} 5%, transparent)`,
        borderRadius: '0 6px 6px 0',
      }}
    >
      {children}
    </div>
  );
}
