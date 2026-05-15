import { Link } from 'react-router-dom';
import { useDashboardStats, useSignals } from '../api/queries';
import { HeroHitRatePanel } from '../components/home/HeroHitRatePanel';
import { HeroOutcomeRow } from '../components/home/HeroOutcomeRow';
import { HowItWorks } from '../components/home/HowItWorks';
import { RegimeTile } from '../components/home/RegimeTile';
import { SignalTypesGrid } from '../components/home/SignalTypesGrid';
import { SignalCard } from '../components/signals';
import {
  Button,
  EmptyState,
  Kicker,
  SectionHeader,
  SkeletonGrid,
} from '../components/ui';
import { narrowPosture, narrowRegime } from '../lib/format';
import { TELEGRAM_BOT_URL } from '../lib/links';

/** Home — HomeV3 (Data-forward) variant.
 *
 * Five sections, dividers via border-b / border-t:
 *   1. Hero (split: copy left, hit-rate panel right)
 *   2. Regime tile (Stage 7-P8 — current Wyckoff phase + posture)
 *   3. Most recent (3-col signal grid)
 *   4. Signal types — 5-cell explainer (what each detector watches for)
 *   5. How it works (3-cell connected)
 *
 * Order rationale: hero pitch → "what's happening right now" (regime +
 * recent) → "what kinds of alerts you'll get" (types) → "how the pipeline
 * works" (how-it-works). A first-time visitor sees current state before
 * marketing copy; a returning user can skip past the explainers.
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
            <p className="text-[16px] leading-[1.5] text-text-2 mb-2 max-w-[520px] font-medium">
              Bound the loss. Let the rest compound.
            </p>
            <p className="text-[14px] leading-[1.5] text-text-3 mb-7 max-w-[520px]">
              Crypto ETF flow signals with published outcomes. AI-explained.
              Delivered to Telegram.
            </p>
            <div className="flex flex-wrap gap-2.5">
              <Button
                as="a"
                href={TELEGRAM_BOT_URL}
                target="_blank"
                rel="noopener noreferrer"
                variant="primary"
              >
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
            // PR B (#60) — read `hit_rate_global` (the v2 name) and fall
            // back to `hit_rate_72h` for the one-cycle window where the
            // backend is on the new code but a stale CDN served an older
            // frontend (the fallback path means a half-rolled deploy
            // doesn't show "—" while both fields carry the same value
            // anyway). Drop the `??` fallback after the next release.
            hitRateGlobal={
              stats.data?.hit_rate_global ?? stats.data?.hit_rate_72h ?? null
            }
            evaluatedCount={stats.data?.evaluated_count ?? null}
          />
        </div>
      </section>

      {/* PR E.2 — hero outcome cards. The whole <section> is wrapped in a
          null-check so cold-start (no qualifying outcomes yet) doesn't
          render an empty band between the hero and the regime tile. */}
      {(stats.data?.last_target_hit || stats.data?.last_stop_saved) && (
        <section className="border-b border-border-2 px-6 sm:px-8 py-8 md:py-10">
          <HeroOutcomeRow
            targetHit={stats.data?.last_target_hit ?? null}
            stopSaved={stats.data?.last_stop_saved ?? null}
          />
        </section>
      )}

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
            hint="Check back shortly — new signals are picked up throughout the day."
          />
        )}
      </section>

      <section className="border-t border-border-2 px-6 sm:px-8 py-14 md:py-16">
        <SectionHeader
          title="Signal types"
          action={
            <span className="text-text-3 text-[13px]">
              Five detectors, one composite regime read.
            </span>
          }
        />
        <SignalTypesGrid />
      </section>

      <section className="border-t border-border-2 px-6 sm:px-8 py-14 md:py-16">
        <SectionHeader title="How it works" />
        <HowItWorks />
      </section>
    </>
  );
}
