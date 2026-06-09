import type { ReactNode } from 'react';

import { Kicker } from './Kicker';

interface SectionHeaderProps {
  title: string;
  /** Optional mono eyebrow rendered above the title (prototype `kicker`). */
  kicker?: ReactNode;
  /** Optional muted sub-line below the title. */
  sub?: ReactNode;
  /** Right-aligned slot — typically a link or Button. */
  action?: ReactNode;
  className?: string;
}

/**
 * Section heading block — reskinned (R1) to the prototype's `SectionHeader`:
 * optional amber kicker, a 20px title, an optional muted sub-line, and a
 * right-aligned action slot. Bottom-aligned so the action sits on the
 * title's baseline. Used for "Most recent / All signals →", "Feed /
 * filters", "Analysis / Share", etc.
 */
export function SectionHeader({ title, kicker, sub, action, className = '' }: SectionHeaderProps) {
  return (
    <div className={`flex items-end justify-between gap-6 mb-[var(--gap)] ${className}`.trim()}>
      <div>
        {kicker && <Kicker className="mb-2">{kicker}</Kicker>}
        <h2 className="text-[20px] font-semibold text-t1" style={{ letterSpacing: '-0.015em' }}>
          {title}
        </h2>
        {sub && (
          <p className="mt-1.5 text-[13px] text-t3 leading-[1.5] max-w-[560px]">{sub}</p>
        )}
      </div>
      {action}
    </div>
  );
}
