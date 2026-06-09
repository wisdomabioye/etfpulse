import type { BreakdownStat } from '../../api/types';
import { EmptyState } from '../ui/EmptyState';
import { BreakdownTable } from './BreakdownTable';

interface BreakdownSectionProps {
  title: string;
  /** One-sentence diagnostic question this breakdown answers — sits
   *  under the title in muted type. Helps readers know *why* they're
   *  looking at this slice. */
  caption?: string;
  rows: BreakdownStat[];
  /** Column header on the categorical column. Defaults to "Category"
   *  via BreakdownTable; set this for every section so the table reads
   *  standalone. */
  labelHeader?: string;
  /** See BreakdownTable.preserveOrder. */
  preserveOrder?: boolean;
}

/**
 * Composes a title + caption + BreakdownTable + empty-state into one
 * section. The Analytics page calls this 4 times — once per categorical
 * dimension — with no duplicated layout code.
 *
 * Empty state is handled INSIDE the section (not by the caller) so the
 * page-level layout stays uniform: every section renders the same
 * vertical rhythm regardless of whether it has data yet.
 */
export function BreakdownSection({
  title,
  caption,
  rows,
  labelHeader,
  preserveOrder = false,
}: BreakdownSectionProps) {
  // Empty-rows test is NOT "rows.every(r => r.total === 0)" — that would
  // hide the backfilled-but-empty confidence buckets which we deliberately
  // want visible (the "9–10 is empty" finding requires showing the empty
  // bucket). We only show the empty state when there are literally zero
  // rows AT ALL (cold-boot before any signals evaluate).
  const isEmpty = rows.length === 0;

  return (
    <section className="bg-bg-2 border border-line-2 rounded-md p-5">
      <header className="mb-3">
        <h3 className="text-[15px] font-semibold text-t1">{title}</h3>
        {caption && <p className="mt-1 text-[12px] text-t3">{caption}</p>}
      </header>
      {isEmpty ? (
        <EmptyState
          title="No data yet"
          hint="Outcomes are evaluated 72h after a signal fires."
        />
      ) : (
        <BreakdownTable
          rows={rows}
          preserveOrder={preserveOrder}
          labelHeader={labelHeader}
        />
      )}
    </section>
  );
}
