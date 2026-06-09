interface LogoProps {
  /** Wordmark font-size in px; the mark scales to `size + 4`. */
  size?: number;
  /** When provided, the logo renders as a button (e.g. nav → home). */
  onClick?: () => void;
  className?: string;
}

/**
 * Brand mark + "ETFPulse" wordmark, ported 1:1 from the prototype's `Logo`.
 * The real `eftpulse_icon.svg` (from /public) carries its own baked-in
 * teal→blue gradient, so it's embedded as an <img> — an external SVG via
 * <img> is colour-isolated (it can't inherit `currentColor`/`var(--acc)`),
 * which is exactly what we want: the brand colours are fixed, not theme-
 * tracked. The wordmark stays as themed text (`text-t1`). Renders as a
 * <button> when `onClick` is given (a11y), otherwise a presentational <span>.
 */
export function Logo({ size = 16, onClick, className = '' }: LogoProps) {
  const mark = (
    <>
      <img
        src="/eftpulse_icon.svg"
        alt=""
        aria-hidden="true"
        // Icon viewBox is 594×512 (wider than tall); fix the height and let
        // width auto-scale so it never squishes. `+6` keeps the mark a touch
        // taller than the cap height for optical balance.
        style={{ height: size + 6, width: 'auto' }}
        className="block shrink-0"
      />
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
