import { Link } from 'react-router-dom';
import { useDashboardStats, useSignals } from '../api/queries';
import { HeroHitRatePanel } from '../components/home/HeroHitRatePanel';
import { HowItWorks } from '../components/home/HowItWorks';
import { RegimeTile } from '../components/home/RegimeTile';
import { SignalCard } from '../components/signals';
import {
  Button,
  EmptyState,
  Kicker,
  SectionHeader,
  SkeletonGrid,
} from '../components/ui';
import { narrowPosture, narrowRegime } from '../lib/format';

/** Home — HomeV3 (Data-forward) variant.
 *
 * Four sections, dividers via border-b / border-t:
 *   1. Hero (split: copy left, hit-rate panel right)
 *   2. Regime tile (Stage 7-P8 — current Wyckoff phase + posture)
 *   3. Most recent (3-col signal grid)
 *   4. How it works (3-cell connected)
 *
 * Horizontal padding is a single pattern: px-6 sm:px-8. No breakpoint chains.
 */
export function Home() {
  const stats = useDashboardStats();
  const recent = useSignals({ limit: 3 });

  // Defensive enum narrowing — see `narrowRegime`/`narrowPosture` in
  // lib/format. RegimeTile renders its own "not yet classified" caption
  // when both are null. Loading is treated as unavailable too — no shimmer
  // in this slot, the caption fits both states honestly.
  const currentRegime = narrowRegime(stats.data?.current_regime);
  const currentPosture = narrowPosture(stats.data?.signal_posture);
  const regimeUnavailable = stats.isError || (!stats.isLoading && currentRegime === null);

  return (
    <>
      <section className="border-b border-border-2 px-6 sm:px-8 py-14 md:py-20">
        <div className="grid grid-cols-1 md:grid-cols-[1.2fr_1fr] gap-10 md:gap-14 items-center">
          <div>
            <Kicker dot className="mb-6">
              Signals intelligence · BTC &amp; ETH ETFs
            </Kicker>
            <h1
              className="text-[44px] md:text-[52px] font-semibold leading-[1.05] mb-4"
              style={{ letterSpacing: '-0.03em', textWrap: 'balance' }}
            >
              Know what the whales are doing.
              <br />
              <span className="text-text-3">Before they move.</span>
            </h1>
            <p className="text-[16px] leading-[1.5] text-text-2 mb-7 max-w-[520px]">
              Crypto ETF flow signals. AI-explained. Delivered to Telegram.
            </p>
            <div className="flex flex-wrap gap-2.5">
              <Button as="a" href="#" variant="primary">
                Open Telegram Bot
                <span className="font-mono opacity-70">↗</span>
              </Button>
              <Button as="link" to="/signals" variant="secondary">
                View feed
              </Button>
            </div>
          </div>

          <HeroHitRatePanel
            signalsToday={stats.data?.signals_today ?? (stats.isError ? null : 0)}
            totalSignals={stats.data?.total_signals ?? (stats.isError ? null : 0)}
            hitRate72h={stats.data?.hit_rate_72h ?? null}
            evaluatedCount={stats.data?.evaluated_count ?? null}
          />
        </div>
      </section>

      <section className="border-b border-border-2 px-6 sm:px-8 py-10 md:py-12">
        <RegimeTile
          regime={currentRegime}
          posture={currentPosture}
          unavailable={regimeUnavailable}
        />
      </section>

      <section className="px-6 sm:px-8 py-14 md:py-16">
        <SectionHeader
          title="Most recent"
          action={
            stats.data ? (
              <Link
                to="/signals"
                className="text-accent text-[13px] font-medium hover:opacity-80"
              >
                All {stats.data.total_signals} signals →
              </Link>
            ) : null
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
              <SignalCard key={s.id} signal={s} compact />
            ))}
          </div>
        ) : (
          <EmptyState
            title="No signals yet."
            hint="Check back after the next daily cycle (04:30 UTC)."
          />
        )}
      </section>

      <section className="border-t border-border-2 px-6 sm:px-8 py-14 md:py-16">
        <SectionHeader title="How it works" />
        <HowItWorks />
      </section>
    </>
  );
}
