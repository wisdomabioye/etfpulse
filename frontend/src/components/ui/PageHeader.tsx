import type { ReactNode } from 'react';

interface PageHeaderProps {
  title: string;
  /** Right-aligned slot — typically a mono meta line or actions. */
  meta?: ReactNode;
  /** Optional above-title kicker (e.g. a Kicker component). */
  eyebrow?: ReactNode;
  className?: string;
}

/**
 * Page-level heading — <h1>, 22px, with optional right-aligned mono meta
 * text and an above-title eyebrow slot. Matches wireframe's feed page
 * header and will back the detail page too.
 */
export function PageHeader({ title, meta, eyebrow, className = '' }: PageHeaderProps) {
  return (
    <div className={className}>
      {eyebrow && <div className="mb-3">{eyebrow}</div>}
      <div className="flex items-baseline justify-between gap-4 flex-wrap">
        <h1
          className="text-[22px] font-semibold text-t1"
          style={{ letterSpacing: '-0.01em' }}
        >
          {title}
        </h1>
        {meta && (
          <div className="font-mono text-[11px] text-t3">{meta}</div>
        )}
      </div>
    </div>
  );
}
