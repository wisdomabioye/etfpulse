import type { ReturnPct } from './outcomeCardHelpers';

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

export function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mb-2">
        {title}
      </div>
      <dl className="m-0 grid grid-cols-[88px_1fr] gap-x-4 gap-y-1.5 font-mono text-[12px]">
        {children}
      </dl>
    </div>
  );
}

export function Row({
  label,
  value,
  secondary,
  toneClass,
}: {
  label: string;
  value: string;
  /** Optional appended pct-return tag, e.g. "+2.4%" with tone color. */
  secondary?: ReturnPct | null;
  /** Optional tone class applied to `value` itself (used by the MARKET
   *  composite path to colour the signed return inline). */
  toneClass?: string;
}) {
  return (
    <>
      <dt className="text-text-3">{label}</dt>
      <dd className={`m-0 tabular-nums break-words ${toneClass ?? 'text-text-1'}`}>
        {value}
        {secondary && (
          <span
            className={`ml-2 font-mono text-[11px] ${secondary.colorClass}`}
            style={{ display: 'inline-block' }}
          >
            {secondary.text}
          </span>
        )}
      </dd>
    </>
  );
}

export function Excursion({
  label,
  value,
  colorClass,
}: {
  label: string;
  value: number | null;
  /** Tailwind color class — `text-pos` for favorable, `text-neg` for adverse. */
  colorClass: string;
}) {
  // value is an unsigned fraction (0.032 = 3.2%) per the backend
  // `_compute_metrics` contract. Render as percent with one decimal.
  const text = value === null ? '—' : `${(value * 100).toFixed(1)}%`;
  return (
    <div>
      <div className="text-[10px] text-text-3 uppercase tracking-[0.1em] mb-1">{label}</div>
      <div className={`text-[14px] tabular-nums ${value === null ? 'text-text-4' : colorClass}`}>
        {text}
      </div>
    </div>
  );
}
