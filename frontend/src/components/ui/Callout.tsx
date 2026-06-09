import type { ReactNode } from 'react';

import type { ColorToken } from '../../lib/colorMix';
import { colorMix, cssVar } from '../../lib/colorMix';

type Tone = 'warn' | 'pos' | 'neg' | 'info';

interface CalloutProps {
  tone?: Tone;
  /** Optional bold heading line above the body. */
  title?: ReactNode;
  /** Optional leading icon, tinted to the tone color. */
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
}

// Tone → design token. `info` maps to the amber accent (prototype convention),
// `pos`/`neg` to win/loss.
const TONE_TOKEN: Record<Tone, ColorToken> = {
  warn: '--warn',
  pos: '--win',
  neg: '--loss',
  info: '--acc',
};

/**
 * Tinted callout — reskinned (R1) to the prototype's `Callout`: a 3px left
 * rule in the tone color over a 6%-tint surface with a 24% border, plus an
 * optional icon + title. The tints are data-driven, so they come through
 * inline `colorMix` / `cssVar`; layout is utility classes.
 */
export function Callout({ tone = 'warn', title, icon, children, className = '' }: CalloutProps) {
  const token = TONE_TOKEN[tone];
  const color = cssVar(token);
  return (
    <div
      className={`flex gap-3 px-4 py-[13px] rounded-md ${className}`.trim()}
      style={{
        background: colorMix(token, 6, cssVar('--bg-2')),
        border: `1px solid ${colorMix(token, 24)}`,
        borderLeft: `3px solid ${color}`,
      }}
    >
      {icon && (
        <span className="shrink-0" style={{ color }}>
          {icon}
        </span>
      )}
      <div>
        {title && <div className="text-[13px] font-semibold mb-[3px] text-t1">{title}</div>}
        <div className="text-[12.5px] text-t2 leading-[1.55]">{children}</div>
      </div>
    </div>
  );
}
