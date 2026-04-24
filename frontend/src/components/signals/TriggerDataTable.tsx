import { Fragment } from 'react';

interface TriggerDataTableProps {
  data: Record<string, unknown>;
}

/**
 * <details>-wrapped definition list for the raw trigger data. 200px dt,
 * 1fr dd, mono font, tabular numerics.
 *
 * Values can be any JSON-serializable type from the detector's
 * `trigger_data` JSONB blob. Primitives render as-is; objects/arrays are
 * JSON.stringified so the reader sees the full structure rather than
 * `[object Object]`. Null/undefined render as "—".
 */
export function TriggerDataTable({ data }: TriggerDataTableProps) {
  const entries = Object.entries(data);

  if (entries.length === 0) {
    return (
      <div className="border border-border-2 rounded-lg bg-bg-2 px-[18px] py-3.5 font-mono text-[13px] text-text-3">
        No trigger data recorded.
      </div>
    );
  }

  return (
    <details
      open
      className="border border-border-2 rounded-lg bg-bg-2 px-[18px] py-3.5 font-mono text-[13px]"
    >
      <summary className="cursor-pointer text-text-3 text-[11px] tracking-[0.1em] uppercase mb-2.5">
        Show raw trigger data
      </summary>
      {/* Below sm: stack dt above dd so the 200px label column doesn't eat
          half the viewport. At sm+ the two-column grid kicks in. */}
      <dl className="m-0 grid gap-x-4 gap-y-2 grid-cols-1 sm:grid-cols-[200px_1fr]">
        {entries.map(([k, v]) => (
          <Fragment key={k}>
            <dt className="text-text-3 mt-2 sm:mt-0">{k.replace(/_/g, ' ')}</dt>
            <dd className="m-0 text-text-1 tabular-nums break-words">{formatValue(v)}</dd>
          </Fragment>
        ))}
      </dl>
    </details>
  );
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
