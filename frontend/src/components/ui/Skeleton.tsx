interface SkeletonProps {
  className?: string;
}

/**
 * Base loading block — reskinned (R1) to the prototype's left-to-right
 * shimmer (bg-3 → bg-4 → bg-3). The `shimmer` keyframe is defined under
 * `prefers-reduced-motion: no-preference` in index.css, so the block sits
 * still (no flashing) for motion-sensitive users. Compose height/width
 * utility classes in the caller.
 */
export function Skeleton({ className = '' }: SkeletonProps) {
  return (
    <div
      className={`rounded-lg ${className}`.trim()}
      style={{
        background: 'linear-gradient(90deg, var(--bg-3) 25%, var(--bg-4) 50%, var(--bg-3) 75%)',
        backgroundSize: '200% 100%',
        animation: 'shimmer 1.4s linear infinite',
      }}
    />
  );
}

interface SkeletonCardProps {
  /** Match the SignalCard compact density so skeletons don't shift layout. */
  compact?: boolean;
  className?: string;
}

export function SkeletonCard({ compact = false, className = '' }: SkeletonCardProps) {
  return <Skeleton className={`${compact ? 'h-24' : 'h-32'} ${className}`.trim()} />;
}

interface SkeletonGridProps {
  /** How many card skeletons to render. */
  count: number;
  /** Tailwind grid-cols chain — caller owns breakpoint responsibility. */
  cols?: string;
  gap?: string;
  compact?: boolean;
}

/**
 * N card skeletons in a responsive grid. Saves the 3-line inline loop
 * that was previously duplicated everywhere a list loads.
 */
export function SkeletonGrid({
  count,
  cols = 'grid-cols-1 md:grid-cols-3',
  gap = 'gap-3.5',
  compact = false,
}: SkeletonGridProps) {
  return (
    <div className={`grid ${cols} ${gap}`}>
      {Array.from({ length: count }, (_, i) => (
        <SkeletonCard key={i} compact={compact} />
      ))}
    </div>
  );
}
