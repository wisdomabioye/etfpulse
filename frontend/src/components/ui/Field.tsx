import type { ReactNode } from 'react';

interface FieldProps {
  label: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * Labelled form field — ported from the prototype's `Field`. A mono
 * uppercase micro-label over the control. Renders a `<label>` so a single
 * contained input is implicitly associated (keeps `getByLabelText` stable);
 * for multi-control groups (Seg / BigToggle) the children carry their own
 * accessible names.
 */
export function Field({ label, children, className = '' }: FieldProps) {
  return (
    <label className={`block ${className}`.trim()}>
      <span className="block font-mono text-[9.5px] text-t4 tracking-[0.1em] uppercase mb-1.5">
        {label}
      </span>
      {children}
    </label>
  );
}
