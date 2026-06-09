import type { SignalOutcome } from '../../api/types';
import { pickVerdict, pctReturn, fmtPriceOrDash, formatCountdown } from './outcomeCardHelpers';
import { Section, Row, Excursion } from './outcomeCardParts';
import { MarketCompositeCard } from './MarketCompositeCard';

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
