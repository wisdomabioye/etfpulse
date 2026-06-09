import type { ReactNode } from 'react';

interface FieldGroupProps {
  label: string;
  children: ReactNode;
  className?: string;
}

/**
 * Labelled wrapper for a GROUP of controls (segmented toggles, the
 * Long/Short side picker) — the multi-control sibling of `Field`.
 *
 * Unlike `Field` it renders a `<div role="group">`, NOT a `<label>`: a
 * `<label>` wrapping more than one control associates with (and folds its
 * text into) the FIRST control, corrupting that control's accessible name
 * so it can't be found by its own label. The visible mono label is a
 * sibling `<span>`, and the group is named via `aria-label`.
 */
export function FieldGroup({ label, children, className = '' }: FieldGroupProps) {
  return (
    <div role="group" aria-label={label} className={`block ${className}`.trim()}>
      <span className="block font-mono text-[9.5px] text-t4 tracking-[0.1em] uppercase mb-1.5">
        {label}
      </span>
      {children}
    </div>
  );
}
