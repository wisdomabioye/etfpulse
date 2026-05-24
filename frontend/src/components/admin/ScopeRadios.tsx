/**
 * `<ScopeRadios>` — global vs per-user radio group for execution
 * halt + resume (#187).
 *
 * The two halt/resume sections needed identical structure: two radio
 * buttons inline, plus an optional per-user input revealed when `scope
 * === 'user'`. Extracted to one place to keep the markup honest.
 *
 * `name` is REQUIRED + must be unique per instance (browser-level
 * radio-group constraint — two radios with the same name on a page
 * become a single group, so two ScopeRadios on the same page MUST
 * use different names).
 *
 * The per-user input is rendered as `children` (a render prop would
 * over-engineer this) — caller supplies an `<IdInput>` already wired
 * to its own state.
 */

import type { ReactNode } from 'react';

export type Scope = 'global' | 'user';

interface Props {
  /** Unique per instance — browser radio-group key. */
  name: string;
  scope: Scope;
  onChange: (next: Scope) => void;
  disabled?: boolean;
  /** Rendered only when `scope === 'user'`. Caller supplies the input. */
  perUserInput?: ReactNode;
}

export function ScopeRadios({
  name,
  scope,
  onChange,
  disabled = false,
  perUserInput,
}: Props) {
  return (
    <div className="flex items-center gap-3 text-[12px]">
      <label className="flex items-center gap-1.5">
        <input
          type="radio"
          name={name}
          checked={scope === 'global'}
          onChange={() => onChange('global')}
          disabled={disabled}
        />
        Global
      </label>
      <label className="flex items-center gap-1.5">
        <input
          type="radio"
          name={name}
          checked={scope === 'user'}
          onChange={() => onChange('user')}
          disabled={disabled}
        />
        Per-user
      </label>
      {scope === 'user' && perUserInput}
    </div>
  );
}
