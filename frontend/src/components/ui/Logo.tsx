interface LogoProps {
  /** Wordmark font-size in px; the mark scales to `size + 4`. */
  size?: number;
  /** When provided, the logo renders as a button (e.g. nav → home). */
  onClick?: () => void;
  className?: string;
}

/**
 * Brand mark + "ETFPulse" wordmark, ported 1:1 from the prototype's `Logo`.
 * The amber square + pulse-line SVG uses `var(--acc)` so it tracks the
 * locked accent. Renders as a <button> when `onClick` is given (a11y — the
 * prototype used a clickable <div>), otherwise a presentational <span>.
 */
export function Logo({ size = 16, onClick, className = '' }: LogoProps) {
  const mark = (
    <>
      <svg width={size + 4} height={size + 4} viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <rect x="1" y="1" width="22" height="22" rx="6" stroke="var(--acc)" strokeWidth="1.5" />
        <path
          d="M5 14 L9 14 L11 7 L14 18 L16 11 L19 11"
          stroke="var(--acc)"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="font-semibold tracking-[-0.02em]" style={{ fontSize: size }}>
        ETFPulse
      </span>
    </>
  );

  const base = `inline-flex items-center gap-[9px] text-t1 ${className}`.trim();

  if (onClick) {
    return (
      <button type="button" onClick={onClick} className={`${base} cursor-pointer`} aria-label="ETFPulse home">
        {mark}
      </button>
    );
  }
  return <span className={base}>{mark}</span>;
}
