import type { SignalOutcome } from '../../api/types';
import { formatUsdPrice } from '../../lib/format';

interface OutcomeCardProps {
  outcome: SignalOutcome | null;
  /** ISO datetime when the signal expires — used to compute "evaluates in Xh". */
  expiresAt: string | null;
}

/**
 * Outcome section of the signal detail page.
 *
 * Two states:
 *   - **Pending** (`outcome === null`): dashed placeholder with a countdown
 *     derived from `expires_at`. Same copy as the home-page hit-rate tile's
 *     pending state for consistency.
 *   - **Evaluated** (Stage 8-P7): hit/miss verdict band + entry/stop/target
 *     levels + +24h / +72h prices with percent returns + max favorable /
 *     adverse excursions. Verdict tone matches the TrackRecord page row
 *     (pos = hit_target, neg = hit_stop, muted = neither / no-target),
 *     so the same outcome looks identical wherever it's surfaced.
 */
export function OutcomeCard({ outcome, expiresAt }: OutcomeCardProps) {
  if (outcome === null) {
    const countdown = expiresAt ? formatCountdown(expiresAt) : null;
    return (
      <div
        className="px-5 py-5 rounded-lg bg-bg-2 text-center font-mono text-[13px] text-text-3"
        style={{ border: '1px dashed var(--color-border-3)' }}
      >
        Pending{countdown ? ` · evaluates in ${countdown}` : ''}
      </div>
    );
  }

  // Verdict tone — `hit_target` wins over `hit_stop` (same convention as
  // TrackRecord page). `null` hit_target means AI didn't volunteer a
  // target, so "neither hit" doesn't apply — we render a muted "—".
  const verdict = pickVerdict(outcome);

  // Returns vs entry. Use `entry_price` (AI-suggested) when set, else
  // `price_at_signal` (live spot at creation) — same fallback as the
  // backend evaluator's `entry_for_metrics` (see `pipeline/track_record.py`)
  // so the rendered % matches the hit/stop computation.
  const entryBaseline = outcome.entry_price ?? outcome.price_at_signal;
  const ret24 = pctReturn(outcome.price_after_24h, entryBaseline);
  const ret72 = pctReturn(outcome.price_after_72h, entryBaseline);

  return (
    <div
      className="rounded-lg bg-bg-2"
      style={{
        border: '1px solid var(--color-border-2)',
        borderLeft: `3px solid ${verdict.color}`,
      }}
    >
      {/* Verdict header */}
      <div
        className="flex items-center justify-between px-5 py-3 border-b border-border-2 font-mono text-[10px] uppercase tracking-[0.1em] text-text-3"
      >
        <span>Outcome</span>
        <span style={{ color: verdict.color }}>{verdict.label}</span>
      </div>

      <div className="px-5 py-4 grid gap-5 grid-cols-1 md:grid-cols-2">
        {/* Left — AI-suggested levels */}
        <Section title="Levels">
          <Row label="Entry" value={fmtPriceOrDash(outcome.entry_price)} />
          <Row label="Stop" value={fmtPriceOrDash(outcome.stop_price)} />
          <Row label="Target" value={fmtPriceOrDash(outcome.target_price)} />
        </Section>

        {/* Right — realised window */}
        <Section title="Realised (vs entry)">
          <Row
            label="At signal"
            value={formatUsdPrice(outcome.price_at_signal)}
            secondary={null}
          />
          <Row
            label="+24h"
            value={fmtPriceOrDash(outcome.price_after_24h)}
            secondary={ret24}
          />
          <Row
            label="+72h"
            value={fmtPriceOrDash(outcome.price_after_72h)}
            secondary={ret72}
          />
        </Section>
      </div>

      {/* Bottom — running excursion. Skip when both null (no kline data). */}
      {(outcome.max_favorable !== null || outcome.max_adverse !== null) && (
        <div className="px-5 pb-4 -mt-1 grid grid-cols-2 gap-5 border-t border-border-2 pt-3 font-mono text-[12px]">
          <Excursion
            label="Max favorable"
            value={outcome.max_favorable}
            colorClass="text-pos"
          />
          <Excursion
            label="Max adverse"
            value={outcome.max_adverse}
            colorClass="text-neg"
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Verdict picker — single source of truth for outcome tone + label.
// ---------------------------------------------------------------------------

interface Verdict {
  label: string;
  color: string;
}

function pickVerdict(o: SignalOutcome): Verdict {
  if (o.hit_target === true) return { label: '✓ Target hit', color: 'var(--color-pos)' };
  if (o.hit_stop === true) return { label: '✗ Stop hit', color: 'var(--color-neg)' };
  if (o.hit_target === null) return { label: '— No target set', color: 'var(--color-text-4)' };
  return { label: '— Neither hit', color: 'var(--color-text-3)' };
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Section({ title, children }: { title: string; children: React.ReactNode }) {
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

function Row({
  label,
  value,
  secondary,
}: {
  label: string;
  value: string;
  /** Optional appended pct-return tag, e.g. "+2.4%" with tone color. */
  secondary?: ReturnPct | null;
}) {
  return (
    <>
      <dt className="text-text-3">{label}</dt>
      <dd className="m-0 text-text-1 tabular-nums break-words">
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

function Excursion({
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

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

interface ReturnPct {
  text: string;
  colorClass: string;
}

/** Computes `(price - baseline) / baseline * 100`, formatted with sign +
 *  tone class. Null when either input is null OR baseline is non-positive
 *  (would divide by zero — same defensive guard as TrackRecord page row). */
function pctReturn(price: number | null, baseline: number): ReturnPct | null {
  if (price === null || baseline <= 0) return null;
  const pct = ((price - baseline) / baseline) * 100;
  return {
    text: `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`,
    colorClass: pct >= 0 ? 'text-pos' : 'text-neg',
  };
}

function fmtPriceOrDash(n: number | null): string {
  return n !== null ? formatUsdPrice(n) : '—';
}



function formatCountdown(iso: string): string | null {
  const target = new Date(iso).getTime();
  if (isNaN(target)) return null;
  const diffMs = target - Date.now();
  if (diffMs <= 0) return 'due now';
  const hours = Math.floor(diffMs / 3_600_000);
  if (hours < 1) return '<1h';
  if (hours < 48) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}
