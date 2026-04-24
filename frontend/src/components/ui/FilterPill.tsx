import type { MouseEventHandler, ReactNode } from 'react';

interface FilterPillProps {
  children: ReactNode;
  active?: boolean;
  onClick?: MouseEventHandler<HTMLButtonElement>;
  className?: string;
}

/**
 * Segmented-filter pill. Mono font, small capsule, active state tinted via
 * bg-3 + border-3 per mock `FilterBar.pill`. Parent owns the `active` state
 * and selection logic — this is a stateless presentational button.
 */
export function FilterPill({
  children,
  active = false,
  onClick,
  className = '',
}: FilterPillProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`px-3 py-[7px] rounded-[5px] text-[12px] font-medium font-mono tracking-[0.02em] border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
        active
          ? 'bg-bg-3 text-text-1 border-border-3'
          : 'bg-transparent text-text-2 border-border-2 hover:text-text-1 hover:border-border-3'
      } ${className}`.trim()}
    >
      {children}
    </button>
  );
}
