import { Link } from 'react-router-dom';
import type { MouseEventHandler, ReactNode } from 'react';

type Variant = 'primary' | 'secondary' | 'ghost';
type Size = 'sm' | 'md';

interface Common {
  variant?: Variant;
  size?: Size;
  className?: string;
  children: ReactNode;
}

type AsButton = Common & {
  as?: 'button';
  type?: 'button' | 'submit' | 'reset';
  onClick?: MouseEventHandler<HTMLButtonElement>;
  disabled?: boolean;
};

type AsAnchor = Common & {
  as: 'a';
  href: string;
  target?: string;
  rel?: string;
};

type AsLink = Common & {
  as: 'link';
  to: string;
};

export type ButtonProps = AsButton | AsAnchor | AsLink;

/**
 * Polymorphic button. Renders as <button>, <a>, or react-router <Link>
 * via the `as` prop — same styling across all three, so /signals filters
 * ("Apply" <button>) and hero CTAs ("Open Telegram" <a>, "View feed"
 * <Link>) share one visual source of truth.
 *
 * `primary` uses the amber accent with dark ink (#07131a) — the one place
 * we step outside the token set because dark-on-amber is a fixed pairing.
 */
export function Button(props: ButtonProps) {
  const { variant = 'primary', size = 'md', className = '', children } = props;

  const base =
    'inline-flex items-center justify-center gap-2 rounded-md transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent';

  const variants: Record<Variant, string> = {
    primary: 'bg-accent font-semibold hover:opacity-90 text-[#07131a]',
    secondary:
      'border border-border-3 text-text-1 font-medium hover:border-text-3',
    ghost: 'text-text-2 font-medium hover:text-text-1',
  };

  const sizes: Record<Size, string> = {
    sm: 'px-3 py-1.5 text-[12px]',
    md: 'px-5 py-3 text-[14px]',
  };

  const cls = `${base} ${variants[variant]} ${sizes[size]} ${className}`.trim();

  if (props.as === 'a') {
    return (
      <a href={props.href} target={props.target} rel={props.rel} className={cls}>
        {children}
      </a>
    );
  }

  if (props.as === 'link') {
    return (
      <Link to={props.to} className={cls}>
        {children}
      </Link>
    );
  }

  return (
    <button
      type={props.type ?? 'button'}
      onClick={props.onClick}
      disabled={props.disabled}
      className={cls}
    >
      {children}
    </button>
  );
}
