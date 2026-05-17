import type { DetectorRow, PerDetectorResponse } from '../../api/types';
import { Kicker, Skeleton } from '../ui';

/**
 * PR I.3 — per-detector precision leaderboard.
 *
 * Answers "which detector should I trust?" Each registered detector gets
 * one row showing across-horizons hit rate, sample count, and a 95%
 * Wilson confidence interval. Cells below `min_samples` show "—" + the
 * sample count so the user can tell apart "no data yet" from "noisy
 * estimate." A perfect detector is far above 50%; a noisy one straddles
 * it.
 *
 * Layout choice (Option C from the I.3 plan):
 *   - Compact table, one row per detector.
 *   - `total` column is the primary readout (across all horizons).
 *   - Per-horizon breakdown is in the response but hidden in v1 — we'll
 *     surface it as an expand-row interaction once any detector reaches
 *     ~15 samples per horizon. Today's data is too thin (typically 5–10
 *     evaluated outcomes per detector across all horizons combined) for
 *     a per-horizon split to be more informative than the aggregate.
 *
 * regime_shift exclusion: the backend filters it out by design — MARKET
 * signals aren't scoreable under the single-asset entry/stop/target
 * rubric. The footnote tells the user where to find that story (PR I.3b,
 * once composite scoring lands). Don't render an empty row for it; that
 * would be the quiet-pretense version of hiding the gap.
 *
 * Numbers from the backend are 0..1 fractions; *100 on render. CI bounds
 * are also fractions; rendered as "[40% – 89%]" with the same scaling.
 */

interface DetectorPrecisionCardProps {
  data: PerDetectorResponse;
  className?: string;
}

export function DetectorPrecisionCard({ data, className }: DetectorPrecisionCardProps) {
  return (
    <section className={`${className ?? ''}`.trim()}>
      <Kicker dot={false}>
        Detector precision · {data.ai_prompt_version} · last {data.lookback_days}d
      </Kicker>
      <h2
        className="text-[20px] font-semibold text-text-1 mt-1.5"
        style={{ letterSpacing: '-0.01em' }}
      >
        Which detector is predictive?
      </h2>
      <p className="mt-1 text-[13px] text-text-3 max-w-[640px]">
        Empirical hit rate per detector, across all horizons. Cells need at
        least {data.min_samples} samples before a rate is shown; below that
        the row reports counts but withholds the rate. Wilson 95% CI shown
        in brackets — bounds far from 50% mean the signal is real even at
        modest sample sizes.
      </p>

      <div className="mt-4 overflow-hidden rounded-lg border border-border-2 bg-bg-2">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="text-text-3 border-b border-border-2">
              <ColHead className="pl-4">Detector</ColHead>
              <ColHead className="text-right">Hit rate</ColHead>
              <ColHead className="text-right">Samples</ColHead>
              <ColHead className="text-right pr-4">95% CI</ColHead>
            </tr>
          </thead>
          <tbody>
            {data.detectors.map((row) => (
              <DetectorPrecisionRow key={row.signal_type} row={row} />
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-3 text-[11px] text-text-4 italic">
        regime_shift is scored separately — pending PR I.3b (MARKET-asset
        composite scoring). Until it lands, regime-shift signals don't have
        comparable hit-rate data on this leaderboard.
      </p>
    </section>
  );
}

/* -------------------------------------------------------------------------
 * Row + cells
 * ------------------------------------------------------------------------- */

function DetectorPrecisionRow({ row }: { row: DetectorRow }) {
  const { hit_rate, n_samples, ci_low, ci_high } = row.total;
  const hasRate = hit_rate !== null;

  return (
    <tr className="border-b border-border-1 last:border-b-0">
      <td className="pl-4 py-3 text-text-1 font-medium">
        {formatSignalTypeLabel(row.signal_type)}
      </td>
      <td className="text-right py-3 font-mono tabular-nums">
        {hasRate ? (
          // Tone: green when the 95% Wilson CI lies entirely above 0.5,
          // red when entirely below, neutral when it straddles. Wider
          // than a simple ">=50%" check — point estimates at small N
          // are misleading (a 70% hit rate at n=4 has a [22%, 95%] CI;
          // the user should act on the bound, not the centerpoint).
          <span className={TONE_CLASS[toneFromCI(ci_low, ci_high)]}>
            {Math.round(hit_rate * 100)}%
          </span>
        ) : (
          <span className="text-text-4">—</span>
        )}
      </td>
      <td className="text-right py-3 font-mono tabular-nums text-text-3">
        n={n_samples}
      </td>
      <td className="text-right pr-4 py-3 font-mono tabular-nums text-text-3">
        {hasRate && ci_low !== null && ci_high !== null ? (
          <span>
            [{Math.round(ci_low * 100)}% – {Math.round(ci_high * 100)}%]
          </span>
        ) : (
          <span className="text-text-4">{n_samples === 0 ? 'no data' : 'pending'}</span>
        )}
      </td>
    </tr>
  );
}

function ColHead({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <th
      className={`py-2.5 font-mono text-[11px] uppercase tracking-[0.08em] font-normal ${className ?? ''}`}
    >
      {children}
    </th>
  );
}

/* -------------------------------------------------------------------------
 * Helpers
 * ------------------------------------------------------------------------- */

type RateTone = 'pos' | 'neg' | 'neutral';

const TONE_CLASS: Record<RateTone, string> = {
  pos: 'text-pos',
  neg: 'text-neg',
  neutral: 'text-text-1',
};

/** Tone is determined by whether the 95% CI strictly excludes 0.5 — the
 * statistical version of "this hit rate is meaningfully different from a
 * coin flip." Caller MUST gate this on `hit_rate !== null` so both
 * `ci_low` and `ci_high` are guaranteed non-null by the backend contract
 * (see `pipeline/per_detector.py:_build_cell`).
 */
function toneFromCI(ci_low: number | null, ci_high: number | null): RateTone {
  if (ci_low === null || ci_high === null) return 'neutral';
  if (ci_low > 0.5) return 'pos';
  if (ci_high < 0.5) return 'neg';
  return 'neutral';
}

/** Format a signal_type for display: `"flow_anomaly"` → `"Flow Anomaly"`.
 * Mirrors `formatSignalType` in lib/format.ts but takes a plain string
 * since `PerDetectorResponse.detectors[].signal_type` is intentionally
 * un-narrowed (legacy/removed detectors can appear).
 */
function formatSignalTypeLabel(signalType: string): string {
  return signalType
    .split('_')
    .map((word) => (word.length > 0 ? word[0].toUpperCase() + word.slice(1) : word))
    .join(' ');
}

/* -------------------------------------------------------------------------
 * Skeleton
 * ------------------------------------------------------------------------- */

export function DetectorPrecisionCardSkeleton({ className }: { className?: string }) {
  return (
    <section className={`${className ?? ''}`.trim()}>
      <Skeleton className="h-3 w-48 mb-2" />
      <Skeleton className="h-6 w-72 mb-1" />
      <Skeleton className="h-4 w-full max-w-[640px] mt-2" />
      <Skeleton className="h-4 w-3/4 max-w-[480px] mt-1" />
      <div className="mt-4 rounded-lg border border-border-2 bg-bg-2">
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            className="flex items-center justify-between px-4 py-3 border-b border-border-1 last:border-b-0"
          >
            <Skeleton className="h-4 w-32" />
            <div className="flex items-center gap-6">
              <Skeleton className="h-4 w-10" />
              <Skeleton className="h-4 w-10" />
              <Skeleton className="h-4 w-24" />
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
