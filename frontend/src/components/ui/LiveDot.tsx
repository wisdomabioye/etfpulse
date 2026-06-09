interface LiveDotProps {
  className?: string;
}

/**
 * Pulsing "live data" dot — ported 1:1 from the prototype's `.live-dot`:
 * GREEN (`--win`), 7px, soft win-tinted glow, `blink 2.4s`. Used in the
 * TopNav status row, the Home hero, and the Signals feed header.
 */
export function LiveDot({ className = '' }: LiveDotProps) {
  return (
    <span
      className={`inline-block w-[7px] h-[7px] rounded-full bg-win shrink-0 ${className}`.trim()}
      style={{
        boxShadow: '0 0 0 0 color-mix(in oklab, var(--win) 60%, transparent)',
        animation: 'blink 2.4s ease-in-out infinite',
      }}
      aria-hidden
    />
  );
}
