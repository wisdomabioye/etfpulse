import type { ReactNode } from 'react';

interface SectionLabelProps {
  /** Leading accent-colored badge — mock uses "01", "02", etc. Free-form string. */
  n?: string;
  children: ReactNode;
  className?: string;
}

/**
 * Mono uppercase divider label — the "01 · AI REASONING" pattern from the
 * detail page. A flex-1 hairline rule fills the remaining width so the
 * label reads as a section separator, not a heading.
 *
 * Different shape from SectionHeader (h2 + action slot) on purpose —
 * SectionHeader introduces a block; SectionLabel divides sub-blocks.
 */
export function SectionLabel({ n, children, className = '' }: SectionLabelProps) {
  return (
    <div
      className={`flex items-center gap-2.5 mb-3.5 font-mono text-[10px] text-text-3 uppercase tracking-[0.12em] ${className}`.trim()}
    >
      {n && <span className="text-accent">{n}</span>}
      <span>{children}</span>
      <span className="flex-1 h-px bg-border-2" aria-hidden />
    </div>
  );
}
