import { Link } from 'react-router-dom';
import type { MouseEventHandler, ReactNode } from 'react';

import { colorMix } from '../../lib/colorMix';

type Variant = 'primary' | 'secondary' | 'ghost' | 'outline';
type Size = 'sm' | 'md' | 'lg';

interface Common {
  variant?: Variant;
  size?: Size;
  /** Stretch to the container width. */
  full?: boolean;
  /** Leading icon node, rendered before the label. */
  icon?: ReactNode;
  /** Loss-toned styling for destructive actions (overrides `variant`). */
  destructive?: boolean;
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

const SIZES: Record<Size, string> = {
  sm: 'px-[11px] py-[6px] text-[12px] gap-1.5',
  md: 'px-[15px] py-[9px] text-[13px] gap-[7px]',
  lg: 'px-5 py-3 text-[14px] gap-2',
};

const VARIANTS: Record<Variant, string> = {
  primary: 'bg-acc text-acc-ink border border-acc font-semibold',
  secondary: 'bg-bg-3 text-t1 border border-line-3 font-medium',
  ghost: 'bg-transparent text-t2 border border-transparent font-medium',
  outline: 'bg-transparent text-t1 border border-line-3 font-medium',
};

const BASE =
  'inline-flex items-center justify-center rounded-sm whitespace-nowrap tracking-[-0.005em] ' +
  'transition-[filter,background-color,color,border-color] duration-[var(--dur-1)] ease-[var(--ease)] hover:brightness-[1.08]';

/**
 * Polymorphic button — renders as <button>, <a>, or react-router <Link>
 * via `as`, so filters, hero CTAs, and nav links share one visual source.
 *
 * Reskinned to the amber design tokens (R1): `primary` is amber-on-ink,
 * `secondary`/`outline` use `bg-3`/`line-3`, `ghost` is borderless. The
 * `destructive` flag swaps to the loss-soft treatment (35% loss border via
 * `colorMix`, the one data-driven color). Focus is handled by the global
 * `:focus-visible` amber ring (index.css), matching the prototype.
 */
export function Button(props: ButtonProps) {
  const {
    variant = 'primary',
    size = 'md',
    full,
    icon,
    destructive,
    className = '',
    children,
  } = props;

  const tone = destructive ? 'bg-loss-soft text-loss border font-semibold' : VARIANTS[variant];
  const cls = [BASE, SIZES[size], tone, full ? 'w-full' : '', className].filter(Boolean).join(' ');

  // Only destructive needs an inline (data-driven) border color.
  const style = destructive ? { borderColor: colorMix('--loss', 35) } : undefined;

  const inner = (
    <>
      {icon}
      {children}
    </>
  );

  if (props.as === 'a') {
    return (
      <a href={props.href} target={props.target} rel={props.rel} className={cls} style={style}>
        {inner}
      </a>
    );
  }

  if (props.as === 'link') {
    return (
      <Link to={props.to} className={cls} style={style}>
        {inner}
      </Link>
    );
  }

  return (
    <button
      type={props.type ?? 'button'}
      onClick={props.onClick}
      disabled={props.disabled}
      className={cls}
      style={style}
    >
      {inner}
    </button>
  );
}
