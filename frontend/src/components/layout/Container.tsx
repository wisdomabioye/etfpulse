import type { ReactNode } from 'react';

interface ContainerProps {
  children: ReactNode;
  className?: string;
  /** 'narrow' caps width at 780px for the signal detail reading layout. */
  narrow?: boolean;
}

/** Horizontal-padding wrapper for sections inside the frame.
 *
 * Page-level max-width lives on the outer shell in App.tsx (max-w-7xl).
 * Inside the frame, sections just need consistent horizontal padding.
 * `narrow` caps at 780px for reading-layout contexts (signal detail).
 */
export function Container({ children, className = '', narrow = false }: ContainerProps) {
  return (
    <div
      className={`${narrow ? 'max-w-[780px] mx-auto ' : ''}px-6 sm:px-8 ${className}`}
    >
      {children}
    </div>
  );
}
