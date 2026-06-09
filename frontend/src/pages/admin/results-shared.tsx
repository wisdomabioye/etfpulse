/**
 * Shared presentational helper for admin action result-display components
 * (#187 split). `ResultLine` is consumed by both results-pipeline and
 * results-execution. If a non-result call site needs it, promote to
 * `components/ui/`.
 */

/** One label/value row inside a result Callout. `tone='warn'` colors the
 *  value yellow + bold to draw the operator's eye when something needs
 *  attention. */
export function ResultLine({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | string;
  tone?: 'warn';
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-t3">{label}</span>
      <span className={tone === 'warn' ? 'text-warn font-semibold' : 'text-t1'}>
        {value}
      </span>
    </div>
  );
}
