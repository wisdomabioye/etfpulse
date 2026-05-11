import { Fragment } from 'react';
import { formatUsdPrice } from '../../lib/format';

interface TriggerDataTableProps {
  data: Record<string, unknown>;
}

/**
 * Clean key-value table for detector trigger data.
 *
 * Lives inside a `<section>` with a `SectionLabel` heading on the detail
 * page, so the table itself carries no second "Show raw trigger data"
 * header (was previously a `<details open>` wrapper — felt redundant and
 * users called out the duplicate heading). Just the table.
 *
 * Keys already surfaced in their own sections are filtered out before
 * rendering so the same data isn't shown twice:
 *   - `news_context`          → rendered by `NewsContextSection`
 *   - `regime_at_creation`    → could be rendered separately; either way
 *                              not as raw JSON in this table
 *
 * Values are formatted by key-shape inference:
 *   - `*_usd` / `*_usd_*`     → `formatUsdPrice(n)` ($1,234.56)
 *   - `change_ratio` / `*_pct`/ `*_ratio` → percent with 2 decimals
 *   - long decimal strings    → trimmed to 4 decimals
 *   - dates / strings         → as-is
 *   - objects / arrays        → JSON.stringify (last resort; should be
 *                              extracted to their own section first)
 *
 * Adding a new formatter rule = a new branch in `formatValue`. Adding a
 * new "render this elsewhere, skip here" rule = a new entry in `SKIP_KEYS`.
 */

const SKIP_KEYS = new Set(['news_context', 'regime_at_creation']);

export function TriggerDataTable({ data }: TriggerDataTableProps) {
  const entries = Object.entries(data).filter(([k]) => !SKIP_KEYS.has(k));

  if (entries.length === 0) {
    return (
      <div className="border border-border-2 rounded-lg bg-bg-2 px-[18px] py-3.5 font-mono text-[13px] text-text-3">
        No additional trigger data recorded.
      </div>
    );
  }

  return (
    <div className="border border-border-2 rounded-lg bg-bg-2 overflow-hidden">
      <dl className="m-0 grid gap-x-4 gap-y-0 grid-cols-1 sm:grid-cols-[200px_1fr]">
        {entries.map(([k, v], i) => (
          <Fragment key={k}>
            <dt
              className={`px-[18px] py-3 font-mono text-[11px] uppercase tracking-[0.05em] text-text-3 ${
                i > 0 ? 'border-t border-border-2 sm:border-t' : ''
              }`}
            >
              {humanizeKey(k)}
            </dt>
            <dd
              className={`m-0 px-[18px] py-3 font-mono text-[13px] text-text-1 tabular-nums break-words ${
                i > 0 ? 'border-t border-border-2 sm:border-t' : ''
              }`}
            >
              {formatValue(k, v)}
            </dd>
          </Fragment>
        ))}
      </dl>
    </div>
  );
}

function humanizeKey(k: string): string {
  // window_days → Window Days; recent_window_sum_usd → Recent Window Sum (USD)
  return k
    .replace(/_usd\b/gi, ' (USD)')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace(/\bUsd\b/g, 'USD')
    .replace(/\bPct\b/g, '%');
}

function formatValue(k: string, v: unknown): string {
  if (v === null || v === undefined) return '—';
  const lk = k.toLowerCase();

  // USD-denominated sums — Decimal strings from JSONB serialization.
  if (lk.includes('_usd') || lk.endsWith('usd')) {
    const n = coerceNumber(v);
    if (n !== null) return formatUsdPrice(n);
  }

  // Ratios / percentages — multiply by 100 unless already a percent-shaped
  // number. `change_ratio` from acceleration is a raw fraction; the AI
  // prompt sees it as-is but humans want %.
  if (lk.includes('change_ratio') || lk.endsWith('_pct') || lk.endsWith('_ratio')) {
    const n = coerceNumber(v);
    if (n !== null) return `${(n * 100).toFixed(2)}%`;
  }

  if (typeof v === 'string') {
    // Long decimal strings: trim to 4 decimals while preserving any
    // leading sign. `12.38178010609971770915613418` → `12.3818`.
    const m = v.match(/^-?\d+\.\d{5,}$/);
    if (m) return Number(v).toFixed(4);
    return v;
  }
  if (typeof v === 'number') {
    return Number.isInteger(v) ? String(v) : v.toFixed(4);
  }
  if (typeof v === 'boolean') return String(v);

  // Last resort — objects/arrays. Anything renderable as its own
  // section should be in SKIP_KEYS and never reach this branch.
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

function coerceNumber(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string') {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}
