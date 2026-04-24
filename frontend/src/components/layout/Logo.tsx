import { Link } from 'react-router-dom';

interface LogoProps {
  /** Font size in px. Mock default is 15 for nav, 12 for footer. */
  size?: number;
  /** When true, renders plain (no Link wrapper) — for footer contexts
   * where the whole area isn't clickable. */
  plain?: boolean;
}

/**
 * Amber `›` glyph + "ETFPulse" wordmark.
 *
 * Matches the mock's primitives.jsx Logo exactly: inline-flex, 8px gap,
 * Inter semibold with -0.01em letter-spacing, mono font on the glyph,
 * accent color on the glyph only. Sized via the `size` prop — glyph is
 * (size + 4) box, text is `size`, glyph font-size is (size - 1).
 */
export function Logo({ size = 15, plain = false }: LogoProps) {
  const inner = (
    <div
      className="inline-flex items-center gap-2 font-sans font-semibold"
      style={{ fontSize: size, letterSpacing: '-0.01em' }}
    >
      <span
        className="inline-flex items-center justify-center font-mono text-accent font-bold"
        style={{ width: size + 4, height: size + 4, fontSize: size - 1 }}
      >
        ›
      </span>
      <span>ETFPulse</span>
    </div>
  );

  if (plain) return inner;
  return (
    <Link to="/" className="text-text-1 no-underline hover:no-underline">
      {inner}
    </Link>
  );
}
