import { useState } from 'react';

import { useAnalyticsBreakdown, useCalibration, usePerDetector, useTrackRecord } from '../api/queries';
import type { AssetSymbol, DetectorRow, SignalType, TimeHorizon } from '../api/types';
import { Breadcrumb } from '../components/layout';
import { Bar, CalibrationCurve, DetectorLeaderboard, HitRateBars } from '../components/charts';
import type { HitRateRow, LeaderboardRow } from '../components/charts';
import { CalibTable } from '../components/proof/CalibTable';
import { OutcomeTable } from '../components/proof/OutcomeTable';
import { ProofStatBand } from '../components/proof/ProofStatBand';
import {
  AssetBadge,
  Button,
  Callout,
  Card,
  EmptyState,
  Kicker,
  SectionHeader,
  Skeleton,
  Tabs,
} from '../components/ui';
import { HORIZONS, SIGNAL_TYPES } from '../lib/constants';
import { calibrationCells } from '../lib/calibrationCells';
import { cssVar } from '../lib/colorMix';
import { formatPct } from '../lib/format';

const HORIZON_TABS = HORIZONS.map((h) => ({ value: h.key, label: h.label }));
const KNOWN_TYPES = new Set<string>(SIGNAL_TYPES);

function toLeaderboard(detectors: DetectorRow[]): LeaderboardRow[] {
  return detectors
    .filter((d) => KNOWN_TYPES.has(d.signal_type) && d.total.hit_rate !== null)
    .map((d) => ({
      key: d.signal_type as SignalType,
      hit: d.total.hit_rate as number,
      ci_low: d.total.ci_low ?? 0,
      ci_high: d.total.ci_high ?? 0,
      n: d.total.n_samples,
    }))
    .sort((a, b) => b.hit - a.hit);
}

/**
 * Proof — the merged credibility surface (R5: track-record + analytics +
 * calibration + per-detector in one page). Every number is real and carries
 * its CI / sample count; nothing is rounded into a single vanity figure.
 */
const LOOKBACK_OPTIONS = [30, 90, 180] as const;

export function Proof() {
  const [horizon, setHorizon] = useState<TimeHorizon>('swing');
  // Cohort lookback — drives the calibration + per-detector queries (the
  // prototype's interactive "lookback" select, wired to the real param).
  const [lookback, setLookback] = useState(90);
  const track = useTrackRecord();
  const calibration = useCalibration({ lookback_days: lookback });
  const perDetector = usePerDetector({ lookback_days: lookback });
  const analytics = useAnalyticsBreakdown();

  const summary = track.data?.summary ?? null;
  const cells = calibration.data ? calibrationCells(calibration.data, horizon) : [];
  const leaderboard = perDetector.data ? toLeaderboard(perDetector.data.detectors) : [];

  const byHorizon: HitRateRow[] = summary
    ? HORIZONS.map((h) => ({ horizon: h.key, pct: summary.hit_rate_by_horizon[h.key] }))
        .filter((x): x is { horizon: TimeHorizon; pct: number } => x.pct !== null)
        .map((x) => ({ horizon: x.horizon, hit: x.pct / 100 }))
    : [];

  const byAsset = (analytics.data?.by_asset ?? []).filter((b) => b.hit_rate_pct !== null);

  return (
    <div className="px-6 pt-8 pb-12 max-w-[1280px] mx-auto">
      <Breadcrumb items={[{ label: 'ETFPulse', path: '/' }, { label: 'Proof' }]} />
      <header className="mb-7">
        <Kicker>The credibility spine</Kicker>
        <h1 className="text-[34px] font-semibold tracking-[-0.03em] mt-3 mb-2.5">
          We publish our misses.
        </h1>
        <p className="text-t2 text-[15px] max-w-[640px] leading-[1.55]">
          Every signal is scored against real Binance / SoSoValue prices at 24h and 72h. This is the
          whole record — calibration, per-detector precision, and per-outcome detail — with
          confidence intervals and sample counts, never rounded into a single vanity number.
        </p>
      </header>

      {/* cohort bar — current cohort (real prompt version + lookback) + cadence */}
      <div className="flex gap-3.5 items-center flex-wrap mb-6 px-4 py-3 bg-bg-1 border border-line-2 rounded-md">
        <span className="font-mono text-[10px] text-t3 tracking-[0.1em] uppercase">Cohort</span>
        <span className="font-mono text-[11px] text-t3">
          prompt <span className="text-t1">{calibration.data?.ai_prompt_version ?? '—'}</span>
        </span>
        <label className="font-mono text-[11px] text-t3 inline-flex items-center gap-1.5">
          lookback
          <select
            value={lookback}
            onChange={(e) => setLookback(Number(e.target.value))}
            aria-label="Calibration lookback window"
            className="font-mono text-[11px] bg-bg-3 text-t1 border border-line-3 rounded-sm px-2 py-1 focus-visible:outline-none"
          >
            {LOOKBACK_OPTIONS.map((d) => (
              <option key={d} value={d}>
                {d} days
              </option>
            ))}
          </select>
        </label>
        <span className="flex-1" />
        <span className="font-mono text-[10px] text-t4">updated nightly · scored vs real prices</span>
      </div>

      {track.isLoading ? (
        <Skeleton className="h-[120px] w-full mb-7" />
      ) : track.isError ? (
        <EmptyState
          title="Couldn't load the track record."
          hint="Check your connection and retry."
          action={
            <Button variant="secondary" size="sm" onClick={() => track.refetch()}>
              Retry
            </Button>
          }
        />
      ) : summary ? (
        <>
          <div className="mb-7">
            <ProofStatBand summary={summary} />
          </div>

          {/* calibration — signature */}
          <Card pad={false} className="mb-6">
            <div className="flex items-start justify-between gap-4 p-5 border-b border-line-2 flex-wrap">
              <div>
                <Kicker>Signature visualization</Kicker>
                <h2 className="text-[20px] font-semibold tracking-[-0.015em] mt-2 mb-1">
                  Calibration reliability curve
                </h2>
                <p className="text-t3 text-[12.5px] max-w-[480px] leading-[1.5]">
                  Observed hit-rate per confidence bucket vs the ideal diagonal. Dots above the line
                  are under-confident; red dots are over-confident. Whiskers are Wilson 95% CIs.
                </p>
              </div>
              <Tabs tabs={HORIZON_TABS} value={horizon} onChange={setHorizon} size="sm" />
            </div>
            <div className="grid grid-cols-1 md:grid-cols-[1.5fr_1fr]">
              <div className="py-5 px-6 md:border-r border-line-2">
                {calibration.isLoading ? (
                  <Skeleton className="h-[320px] w-full" />
                ) : (
                  <CalibrationCurve cells={cells} horizon={horizon} height={320} />
                )}
              </div>
              <div className="py-5 px-6">
                <div className="font-mono text-[10px] text-t3 tracking-[0.1em] uppercase mb-3.5">
                  Bucket detail · {horizon}
                </div>
                {cells.length > 0 && <CalibTable cells={cells} />}
                <div className="mt-4">
                  <Callout tone="info" title="Reading this">
                    Cells with n &lt; {calibration.data?.min_samples ?? '—'} render “—” — we
                    won&apos;t claim a rate we can&apos;t support. The closer the amber line tracks
                    the dashed diagonal, the better-calibrated the model.
                  </Callout>
                </div>
              </div>
            </div>
          </Card>

          {/* per-detector + by-horizon / by-asset */}
          <div className="grid grid-cols-1 lg:grid-cols-[1.3fr_1fr] gap-6 mb-6">
            <Card>
              <SectionHeader
                kicker="Which detector to trust"
                title="Per-detector precision"
                sub="Empirical hit-rate with 95% CI. Green when the lower bound clears 50%; red when the upper bound can't."
              />
              {leaderboard.length ? (
                <DetectorLeaderboard data={leaderboard} />
              ) : (
                <p className="text-t3 text-[13px]">Not enough scored signals per detector yet.</p>
              )}
            </Card>
            <Card>
              <SectionHeader kicker="By horizon" title="Hit-rate by horizon" />
              {byHorizon.length ? (
                <HitRateBars data={byHorizon} />
              ) : (
                <p className="text-t3 text-[13px]">No horizon has scored signals yet.</p>
              )}
              {byAsset.length > 0 && (
                <div className="mt-5 pt-4 border-t border-line-1">
                  <div className="font-mono text-[10px] text-t3 tracking-[0.1em] uppercase mb-3">
                    By asset
                  </div>
                  {byAsset.map((b) => {
                    const hit = (b.hit_rate_pct as number) / 100;
                    return (
                      <div
                        key={b.label}
                        className="grid items-center gap-2.5 mb-2.5"
                        style={{ gridTemplateColumns: '60px 1fr 70px' }}
                      >
                        <AssetBadge asset={b.label as AssetSymbol} size="sm" />
                        <Bar value={hit} color={cssVar(hit >= 0.7 ? '--win' : '--conf-mid')} height={8} />
                        <span className="font-mono tabular-nums text-[12px] text-right font-semibold">
                          {formatPct(hit)}{' '}
                          <span className="text-t4 font-normal text-[9px]">n={b.total}</span>
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>
          </div>

          {/* per-outcome receipts */}
          <Card pad={false}>
            <div className="flex justify-between items-center p-5 border-b border-line-2">
              <SectionHeader kicker="Receipts" title="Per-outcome detail" className="mb-0" />
              <span className="font-mono text-[11px] text-t4">
                last {track.data?.items.length ?? 0} · scored vs real prices
              </span>
            </div>
            <OutcomeTable rows={track.data?.items ?? []} />
          </Card>
        </>
      ) : (
        <EmptyState
          title="No scored signals yet."
          hint="Outcomes appear here once signals age past their validity window."
        />
      )}
    </div>
  );
}
