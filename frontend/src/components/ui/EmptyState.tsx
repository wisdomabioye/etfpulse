import type { ReactNode } from 'react';

interface EmptyStateProps {
  title: string;
  /** Mono-font sub-line with extra context — typically the "why" or next step. */
  hint?: string;
  /** Optional action slot rendered below the hint (e.g. "Clear filters" Button). */
  action?: ReactNode;
  className?: string;
}

/**
 * Empty-state card. Used for:
 *   - Home "Most recent" before the first daily cycle
 *   - /signals feed when a filter has no matches
 *   - /signals/:id 404 fallback
 *   - Bot-not-connected CTAs
 */
export function EmptyState({ title, hint, action, className = '' }: EmptyStateProps) {
  return (
    <div
      className={`border border-border-2 bg-bg-2 rounded-lg px-6 py-10 text-center ${className}`}
    >
      <div className="text-[14px] text-text-2 mb-1">{title}</div>
      {hint && <div className="text-[12px] font-mono text-text-3">{hint}</div>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}
