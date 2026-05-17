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

  // PR I.3b — MARKET (regime_shift) outcomes carry the composite story
  // in `composite_return_pct` (signed fraction); single-asset baseline
  // fields are all NULL by design. Render the dedicated composite block
  // BEFORE running `pickVerdict` — the single-asset verdict picker would
  // emit "Target hit" / "Neither hit" framing that doesn't apply to a
  // MARKET row (no target was ever set). MarketCompositeCard computes
  // its own composite-specific verdict.
  if (outcome.composite_return_pct !== null) {
    return <MarketCompositeCard outcome={outcome} />;
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

  // PR B (#60) — horizon-aware checkpoint rows. The legacy +24h/+72h rows
  // render whenever the window includes them (or for grandfathered rows
  // where `window_hours` is NULL). The new validity-end row appears for
  // horizons whose end falls outside the legacy 72h checkpoint
  // (position 168h today, scalp 6h once #62 lands).
  const windowHours = outcome.window_hours;
  const isLegacyScoring = outcome.scoring_version === null;
  const isScalp = windowHours !== null && windowHours < 24;
  // Show the legacy two-row block when the window is wide enough to
  // include both checkpoints (24h+72h), or when grandfathered (window
  // unknown — render as before so the long tail of pre-PR-B rows looks
  // unchanged).
  const showLegacyRows = windowHours === null || windowHours >= 72;
  // Show a third "+Nh" row when validity end is outside the 72h checkpoint
  // AND we have a value. Skips swing (window=72, same as +72h checkpoint).
  const showValidityEndRow =
    !isScalp &&
    windowHours !== null &&
    windowHours !== 72 &&
    outcome.price_at_validity_end !== null;
  const retValidityEnd = showValidityEndRow
    ? pctReturn(outcome.price_at_validity_end, entryBaseline)
    : null;
  // Scalp under PR B is bucketed but unscored (#62 pending intraday
  // klines). Render the "+6h" row so users see the shape; the price +
  // return will be "—" until #62 lands.
  const retScalp = isScalp
    ? pctReturn(outcome.price_at_validity_end, entryBaseline)
    : null;

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
        <span className="inline-flex items-center gap-2">
          Outcome
          {/* PR B (#60) — surface grandfathered rows so users know this
              outcome was scored against the fixed 72h window, not the
              v2 per-horizon rubric. Pre-cleanup (#61) the badge tells
              the operator + user the methodology disclosure honestly. */}
          {isLegacyScoring && (
            <span
              className="font-mono text-[9px] tracking-normal normal-case text-text-3 px-1.5 py-0.5 rounded"
              style={{ border: '1px solid var(--color-border-3)' }}
              title="Scored against the legacy 72h window — pre-v2 rubric"
            >
              legacy 72h
            </span>
          )}
        </span>
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
            value={fmtPriceOrDash(outcome.price_at_signal)}
            secondary={null}
          />
          {showLegacyRows && (
            <>
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
            </>
          )}
          {showValidityEndRow && (
            <Row
              label={`+${windowHours}h`}
              value={fmtPriceOrDash(outcome.price_at_validity_end)}
              secondary={retValidityEnd}
            />
          )}
          {isScalp && (
            <Row
              label={`+${windowHours}h`}
              value={fmtPriceOrDash(outcome.price_at_validity_end)}
              secondary={retScalp}
            />
          )}
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
function pctReturn(price: number | null, baseline: number | null): ReturnPct | null {
  if (price === null || baseline === null || baseline <= 0) return null;
  const pct = ((price - baseline) / baseline) * 100;
  return {
    text: `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`,
    colorClass: pct >= 0 ? 'text-pos' : 'text-neg',
  };
}

function fmtPriceOrDash(n: number | null): string {
  return n !== null ? formatUsdPrice(n) : '—';
}



// ---------------------------------------------------------------------------
// MARKET (regime_shift) composite outcome — PR I.3b
// ---------------------------------------------------------------------------
//
// MARKET signals are scored as a weighted BTC+ETH composite return (delta)
// rather than against a single-asset entry/stop/target. All single-asset
// baseline fields are NULL by design (PR I.3b migration relaxed
// `price_at_signal` to nullable). Render the composite story directly:
// a verdict band + the signed return + the max_favorable/max_adverse
// excursions (which the evaluator still computes on the same composite
// series). No "Levels" or "+24h/+72h" rows — they don't apply.
function MarketCompositeCard({ outcome }: { outcome: SignalOutcome }) {
  // composite_return_pct is a signed fraction (0.024 = +2.4%). Format with
  // sign and one decimal, same precision as the bot's recent-outcome line.
  const pct = (outcome.composite_return_pct ?? 0) * 100;
  const sign = pct >= 0 ? '+' : '';
  const compositeText = `${sign}${pct.toFixed(2)}%`;
  const toneClass = pct >= 0 ? 'text-pos' : 'text-neg';
  // Composite-specific verdict — mirrors the bot's `_outcome_icon_and_verdict`
  // MARKET branch. "composite hit" / "composite miss" wording rather than
  // the single-asset "Target hit" / "Neither hit" framing (no target ever
  // existed on a MARKET row).
  const verdict: Verdict =
    outcome.hit_target === true
      ? { label: '✓ Composite hit', color: 'var(--color-pos)' }
      : { label: '— Composite miss', color: 'var(--color-text-3)' };
  return (
    <div
      className="rounded-lg bg-bg-2"
      style={{
        border: '1px solid var(--color-border-2)',
        borderLeft: `3px solid ${verdict.color}`,
      }}
    >
      <div className="flex items-center justify-between px-5 py-3 border-b border-border-2 font-mono text-[10px] uppercase tracking-[0.1em] text-text-3">
        <span className="inline-flex items-center gap-2">
          Outcome
          <span
            className="font-mono text-[9px] tracking-normal normal-case text-text-3 px-1.5 py-0.5 rounded"
            style={{ border: '1px solid var(--color-border-3)' }}
            title="Scored as a weighted BTC+ETH composite return (not against a single-asset target)"
          >
            market composite
          </span>
        </span>
        <span style={{ color: verdict.color }}>{verdict.label}</span>
      </div>
      <div className="px-5 py-4 grid gap-5 grid-cols-1 md:grid-cols-2">
        <Section title="Composite return">
          <Row label="BTC+ETH" value={compositeText} secondary={null} toneClass={toneClass} />
        </Section>
        <Section title="Excursion">
          <Row
            label="Max favorable"
            value={outcome.max_favorable === null ? '—' : `${(outcome.max_favorable * 100).toFixed(1)}%`}
            secondary={null}
          />
          <Row
            label="Max adverse"
            value={outcome.max_adverse === null ? '—' : `${(outcome.max_adverse * 100).toFixed(1)}%`}
            secondary={null}
          />
        </Section>
      </div>
    </div>
  );
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
