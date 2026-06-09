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
      className={`px-[11px] py-1.5 rounded-sm text-[12px] font-mono tracking-[0.02em] border transition-colors duration-[var(--dur-1)] ${
        active
          ? 'bg-acc-soft text-acc-hi border-acc-line'
          : 'bg-transparent text-t2 border-line-2 hover:text-t1 hover:border-line-3'
      } ${className}`.trim()}
    >
      {children}
    </button>
  );
}
