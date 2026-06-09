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
      className={`border border-dashed border-line-3 bg-bg-1 rounded-lg px-6 py-12 text-center ${className}`.trim()}
    >
      <div className="text-[14px] font-semibold text-t1 mb-1.5">{title}</div>
      {hint && (
        <div
          className={`text-[13px] text-t3 max-w-[360px] mx-auto leading-[1.5] ${action ? 'mb-4' : ''}`.trim()}
        >
          {hint}
        </div>
      )}
      {action && <div className="flex justify-center">{action}</div>}
    </div>
  );
}
