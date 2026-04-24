import type { ReactNode } from 'react';

interface ContainerProps {
  children: ReactNode;
  className?: string;
  /** Max-width override — defaults to 1200px (dashboard) but the detail
   * page uses 780px per the mock. */
  maxWidth?: 'default' | 'narrow';
}

/**
 * Centered max-width wrapper with responsive horizontal padding.
 *
 * - default: 1200px max (home, feed)
 * - narrow:  780px max (signal detail reading layout, per mock spec)
 *
 * Horizontal padding: 20px on mobile, 28px on tablet+, matching the mock's
 * TopNav padding so content aligns with the nav items.
 */
export function Container({ children, className = '', maxWidth = 'default' }: ContainerProps) {
  const maxW = maxWidth === 'narrow' ? 'max-w-[780px]' : 'max-w-[1200px]';
  return (
    <div className={`${maxW} mx-auto px-5 md:px-7 ${className}`}>
      {children}
    </div>
  );
}
