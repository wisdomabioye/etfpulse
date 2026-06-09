import type { ReactNode } from 'react';

interface PageProps {
  children: ReactNode;
  /** Wide layout caps at 1280px (Proof / Execute); default 1200px. */
  wide?: boolean;
  className?: string;
}

/**
 * Page body wrapper — ported from the prototype's `Page`. Centers content at
 * 1200px (or 1280 when `wide`) with the prototype's `32px 24px 48px` padding
 * and a 60vh min-height so short pages don't collapse the footer upward.
 */
export function Page({ children, wide = false, className = '' }: PageProps) {
  return (
    <main
      className={`mx-auto px-6 pt-8 pb-12 min-h-[60vh] ${wide ? 'max-w-[1280px]' : 'max-w-[1200px]'} ${className}`.trim()}
    >
      {children}
    </main>
  );
}
