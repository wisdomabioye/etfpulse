import type { ReactNode } from 'react';

import { SectionLabel } from '../ui';

interface SectionProps {
  id: string;
  /** Two-digit zero-padded index, e.g. "03". Driven by the page. */
  n: string;
  title: string;
  children: ReactNode;
}

/**
 * Methodology page section wrapper. Pairs SectionLabel (the mono
 * divider) with an h2 + body slot. The page renders one per entry
 * in {@link ./sections.tsx} so structural drift between the TOC
 * and the body is impossible by construction.
 *
 * Lives under `components/methodology/` rather than inline because:
 *   - Methodology.tsx is the primary consumer today, but
 *   - the next long-form page (e.g. /security) will want the same
 *     primitive,
 *   - and keeping the page file under 200 LOC is the discipline.
 */
export function Section({ id, n, title, children }: SectionProps) {
  return (
    <section id={id} className="mt-10 scroll-mt-8">
      <SectionLabel n={n}>{title}</SectionLabel>
      <h2
        className="text-[20px] font-semibold text-text-1"
        style={{ letterSpacing: '-0.01em' }}
      >
        {title}
      </h2>
      <div className="mt-3 text-text-2 leading-[1.65] space-y-3">
        {children}
      </div>
    </section>
  );
}
