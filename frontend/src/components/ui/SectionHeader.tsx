import type { ReactNode } from 'react';

interface SectionHeaderProps {
  title: string;
  /** Right-aligned slot — typically a link or Button. */
  action?: ReactNode;
  className?: string;
}

/**
 * Section title + optional right-aligned action (link or button).
 * Used for "Most recent / All signals →", "Feed / filters",
 * "Analysis / Share", etc.
 */
export function SectionHeader({ title, action, className = '' }: SectionHeaderProps) {
  return (
    <div className={`flex items-baseline justify-between gap-4 mb-5 ${className}`}>
      <h2
        className="text-[20px] font-semibold text-text-1"
        style={{ letterSpacing: '-0.01em' }}
      >
        {title}
      </h2>
      {action}
    </div>
  );
}
