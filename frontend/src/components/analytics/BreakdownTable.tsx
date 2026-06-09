import type { BreakdownStat } from '../../api/types';

interface BreakdownTableProps {
  rows: BreakdownStat[];
  /** When true, render rows in the order received (used for confidence
   *  buckets where 1–3/4–6/7–8/9–10 has natural meaning). Default sorts
   *  by `total` DESC so the largest cohort — i.e. the most statistically
   *  trustworthy row — leads. */
  preserveOrder?: boolean;
  /** Column header for the categorical column (e.g. "Detector", "Asset").
   *  Defaults to "Category" — set this for every section to make the
   *  table standalone-readable. */
  labelHeader?: string;
}

/**
 * Pure presentational table for one categorical breakdown.
 *
 * Four columns: label · cohort size · hits/targeted · hit rate.
 *
 * The `targeted` column matters because `hit_rate_pct = hits / targeted`
 * (NOT `hits / total`) — see backend `compute_hit_rate_pct`. Surfacing
 * the raw `hits / targeted` ratio next to the percent lets readers
 * eyeball the statistical weight of the row ("5/6 = 83%" reads
 * different from "50/60 = 83%").
 */
export function BreakdownTable({
  rows,
  preserveOrder = false,
  labelHeader = 'Category',
}: BreakdownTableProps) {
  const displayRows = preserveOrder ? rows : [...rows].sort((a, b) => b.total - a.total);

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[13px] tabular-nums">
        <thead>
          <tr className="text-t3 text-left font-mono text-[11px] uppercase tracking-[0.08em] border-b border-line-2">
            <th className="py-2 pr-3 font-normal">{labelHeader}</th>
            <th className="py-2 px-3 font-normal text-right">n</th>
            <th className="py-2 px-3 font-normal text-right">Hits / Targeted</th>
            <th className="py-2 pl-3 font-normal text-right">Hit rate</th>
          </tr>
        </thead>
        <tbody>
          {displayRows.map((row) => (
            <BreakdownRow key={row.label} row={row} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Single row — kept inline because it's used nowhere else and the
 *  shared layout decisions (alignment, mono numerics, null rendering)
 *  belong with the table component. Promoting to its own file would
 *  fragment the unit. */
function BreakdownRow({ row }: { row: BreakdownStat }) {
  // `hit_rate_pct === null` means `targeted === 0` — no signals in this
  // cohort had a target, so "hit rate" is undefined, not 0%. Render an
  // em-dash. Matches the null-rendering convention used on TrackRecord.
  const hitRateDisplay =
    row.hit_rate_pct === null ? '—' : `${row.hit_rate_pct.toFixed(1)}%`;
  // Hits-and-targeted column collapses to "—" when the cohort had no
  // targeted signals at all (otherwise we'd render "0 / 0" which reads
  // like a calculation error).
  const hitsTargetedDisplay = row.targeted === 0 ? '—' : `${row.hits} / ${row.targeted}`;

  return (
    <tr className="border-b border-line-2/60 last:border-b-0">
      <td className="py-2.5 pr-3 text-t1">{row.label}</td>
      <td className="py-2.5 px-3 text-right text-t2">{row.total}</td>
      <td className="py-2.5 px-3 text-right text-t2">{hitsTargetedDisplay}</td>
      <td className="py-2.5 pl-3 text-right text-t1 font-medium">{hitRateDisplay}</td>
    </tr>
  );
}
