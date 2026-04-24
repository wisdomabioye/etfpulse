interface SkeletonProps {
  className?: string;
}

/**
 * Base pulse block — a card-shaped placeholder that matches the bg-2
 * surface tone so loading states don't flash brighter than real content.
 * Compose with height/width utility classes in the caller.
 */
export function Skeleton({ className = '' }: SkeletonProps) {
  return (
    <div
      className={`bg-bg-2 border border-border-2 rounded-lg animate-pulse ${className}`}
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
