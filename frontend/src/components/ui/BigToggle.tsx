import type { ReactNode } from 'react';

import type { ColorToken } from '../../lib/colorMix';
import { colorMix, cssVar } from '../../lib/colorMix';

interface BigToggleProps {
  on: boolean;
  /** Design token for the active tint (e.g. `--win` for Long, `--loss` for Short). */
  token: ColorToken;
  /** Decorative leading glyph (▲ / ▼), `aria-hidden`. */
  glyph?: ReactNode;
  /** Accessible name + visible label (e.g. "Long"). */
  label: string;
  onClick: () => void;
  className?: string;
}

/**
 * Large two-state toggle — ported from the prototype's `BigToggle`. Used
 * for the Long / Short side picker. The active tint is data-driven (win /
 * loss), so the fill / border / text come through inline `colorMix` /
 * `cssVar`; the rest is utility classes. Renders a real `<button
 * aria-pressed>` named by `label`.
 */
export function BigToggle({ on, token, glyph, label, onClick, className = '' }: BigToggleProps) {
  const color = cssVar(token);
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`flex-1 py-2.5 rounded-sm font-mono text-[13px] font-semibold inline-flex items-center justify-center gap-1.5 border transition-[background-color,color,border-color] duration-[var(--dur-1)] ${className}`.trim()}
      style={{
        background: on ? colorMix(token, 14) : cssVar('--bg-1'),
        color: on ? color : cssVar('--t3'),
        borderColor: on ? colorMix(token, 40) : cssVar('--line-2'),
      }}
    >
      {glyph && <span aria-hidden="true">{glyph}</span>}
      {label}
    </button>
  );
}
