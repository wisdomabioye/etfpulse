import { useNavigate } from 'react-router-dom';

import { useDashboardStats, useSignals } from '../api/queries';
import { CalibrationTeaser } from '../components/home/CalibrationTeaser';
import { DetectorsShowcase } from '../components/home/DetectorsShowcase';
import { HeroProofCard } from '../components/home/HeroProofCard';
import { LoopDiagram } from '../components/home/LoopDiagram';
import { SignalCardMini } from '../components/signal';
import { Page } from '../components/layout';
import { Button, EmptyState, LiveDot, SectionHeader, SkeletonGrid } from '../components/ui';
import { useLiveNumber } from '../hooks/useLiveNumber';
import { formatConfidence } from '../lib/format';

/**
 * Home — proof-forward hero + the loop (R4 reskin of the prototype's
 * `ScreenHome`).
 *
 *   1. Hero: pitch + live proof-stats strip + the two latest real outcomes
 *      (last target hit / last stop saved) as the headline proof.
 *   2. Most recent — 3-up `SignalCardMini` from the real feed.
 *   3. The disciplined loop (six-stage spine).
 *   4. Five detectors, distinct identities.
 *   5. Calibration teaser → the Proof surface.
 *
 * All numbers are real (`useDashboardStats` / `useSignals` / `useCalibration`);
 * `hit_rate_global` is already a percent, so it feeds `useLiveNumber` directly.
 */
export function Home() {
  const navigate = useNavigate();
  const stats = useDashboardStats();
  const recent = useSignals({ limit: 3 });

  const hitRate = stats.data?.hit_rate_global ?? null;
  const easedHitRate = useLiveNumber(hitRate ?? 0, { live: true });

  const targetHit = stats.data?.last_target_hit ?? null;
  const stopSaved = stats.data?.last_stop_saved ?? null;

  const proofStats: Array<{ label: string; value: string; accent?: boolean }> = [
    { label: '72h hit rate', value: hitRate === null ? '—' : `${easedHitRate.toFixed(1)}%`, accent: true },
    { label: 'Signals scored', value: String(stats.data?.evaluated_count ?? '—') },
    { label: 'Avg confidence', value: formatConfidence(stats.data?.avg_confidence ?? null) },
  ];

  return (
    <>
      {/* HERO */}
      <section
        className="border-b border-line-2"
        style={{
          background:
            'radial-gradient(120% 100% at 50% -20%, color-mix(in oklab, var(--acc) 7%, transparent), transparent 60%)',
        }}
      >
        <div className="max-w-[1200px] mx-auto px-6 pt-16 pb-[52px]">
          <div className="flex items-center gap-2.5 mb-6">
            <LiveDot />
            <span className="font-mono text-[11px] text-t3 tracking-[0.1em] uppercase">
              {stats.data?.signals_today ?? 0} signals today · {stats.data?.evaluated_count ?? 0}{' '}
              scored vs real prices
            </span>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-[1.25fr_1fr] gap-10 lg:gap-12 items-center">
            <div>
              <h1
                className="text-[44px] sm:text-[56px] font-semibold leading-[1.02] mb-5"
                style={{ letterSpacing: '-0.035em', textWrap: 'balance' }}
              >
                Bound the loss.
                <br />
                <span className="text-t3">Let the rest compound.</span>
              </h1>
              <p className="text-t2 text-[18px] leading-[1.5] mb-8 max-w-[520px]">
                Calibrated crypto-ETF flow signals — AI-explained, confirmation-scored, and{' '}
                <span className="text-t1">proven against real prices</span>. We publish our misses.
              </p>
              <div className="flex flex-wrap gap-3">
                <Button as="link" to="/track-record" variant="primary" size="lg">
                  See the track record
                </Button>
                <Button as="link" to="/signals" variant="outline" size="lg">
                  Browse live signals
                </Button>
              </div>

              <div className="flex mt-10 border border-line-2 rounded-lg overflow-hidden bg-bg-2">
                {proofStats.map((s, i) => (
                  <div
                    key={s.label}
                    className="flex-1 px-[18px] py-[15px]"
                    style={{ borderRight: i < proofStats.length - 1 ? '1px solid var(--line-1)' : 'none' }}
                  >
                    <div className="font-mono text-[9.5px] text-t4 tracking-[0.1em] uppercase mb-[7px]">
                      {s.label}
                    </div>
                    <div
                      className={`tabular-nums text-[24px] font-semibold ${s.accent ? 'text-win' : 'text-t1'}`}
                      style={{ letterSpacing: '-0.02em' }}
                    >
                      {s.value}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="flex flex-col gap-3.5">
              {targetHit && <HeroProofCard outcome={targetHit} kind="target" />}
              {stopSaved && <HeroProofCard outcome={stopSaved} kind="stop" />}
              {!targetHit && !stopSaved && (
                <div className="border border-dashed border-line-3 bg-bg-1 rounded-lg p-8 text-center text-t3 text-[13px] leading-[1.5]">
                  Scored outcomes appear here once the first signals age past their validity window.
                </div>
              )}
            </div>
          </div>
        </div>
      </section>

      <Page>
        {/* recent */}
        <section className="mb-14">
          <SectionHeader
            kicker="Live feed"
            title="Most recent signals"
            action={
              <Button as="link" to="/signals" variant="ghost" size="sm">
                All {stats.data?.total_signals ?? ''} signals →
              </Button>
            }
          />
          {recent.isLoading ? (
            <SkeletonGrid count={3} />
          ) : recent.isError ? (
            <EmptyState
              title="Couldn't load recent signals."
              hint="Check your connection and retry."
              action={
                <Button variant="secondary" size="sm" onClick={() => recent.refetch()}>
                  Retry
                </Button>
              }
            />
          ) : recent.data?.items.length ? (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3.5">
              {recent.data.items.map((s) => (
                <SignalCardMini key={s.id} signal={s} onClick={() => navigate(`/signals/${s.id}`)} />
              ))}
            </div>
          ) : (
            <EmptyState
              title="No signals yet."
              hint="Check back shortly — new signals are picked up throughout the day."
            />
          )}
        </section>

        {/* the loop */}
        <section className="mb-14">
          <SectionHeader
            kicker="The spine"
            title="One disciplined loop"
            sub="Every signal is delivered, executed under your signature, then scored against real prices — feeding the track record that earns the next signal's trust."
          />
          <LoopDiagram />
        </section>

        {/* detectors */}
        <section className="mb-14">
          <SectionHeader
            kicker="Intelligence"
            title="Five detectors, distinct identities"
            sub="Each catches a different structure in the flow data. Per-detector precision is published on the track record."
          />
          <DetectorsShowcase />
        </section>

        {/* proof teaser */}
        <section>
          <CalibrationTeaser />
        </section>
      </Page>
    </>
  );
}
