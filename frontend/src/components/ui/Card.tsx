import type { HTMLAttributes, ReactNode } from 'react';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
  /** Apply the standard 20px card padding (`--pad-card`). */
  pad?: boolean;
  /** Lift + amber border on hover (interactive cards). CSS-driven, not JS. */
  hover?: boolean;
  /** 3px amber left rule for emphasis. */
  accent?: boolean;
}

/**
 * Surface card — ported from the prototype's `Card`. `bg-2` surface,
 * `line-2` border (→ `acc-line` + lift on hover when `hover`), `--r-lg`
 * radius. The prototype's `useState(hover)` is replaced by CSS `hover:`
 * (same pixels, better mechanism — no re-render on pointer move).
 */
export function Card({
  children,
  pad = true,
  hover = false,
  accent = false,
  className = '',
  ...rest
}: CardProps) {
  const cls = [
    'bg-bg-2 border border-line-2 rounded-lg',
    'transition-[border-color,transform] duration-[var(--dur-1)] ease-[var(--ease)]',
    pad ? 'p-5' : '',
    hover ? 'hover:border-acc-line hover:-translate-y-px' : '',
    accent ? 'border-l-[3px] border-l-acc' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className={cls} {...rest}>
      {children}
    </div>
  );
}
