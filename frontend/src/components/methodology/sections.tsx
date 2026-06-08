import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';

/**
 * Methodology page content — pure data, no UI components defined
 * here. Body slots return ReactNode so internal Links + emphasis
 * markup live next to the prose that uses them.
 *
 * Single source of truth for the page's structure. The page's
 * table-of-contents and the rendered body both iterate the same
 * array, so a new section requires exactly one edit.
 *
 * Every factual claim is grounded in CLAUDE.md (the internal
 * codebase guide). When config defaults or behavior change there,
 * update the matching line here in the same change set to keep
 * methodology aligned with what production actually does.
 */

export interface SectionContent {
  id: string;
  title: string;
  body: ReactNode;
}

export const METHODOLOGY_SECTIONS: SectionContent[] = [
  {
    id: 'data-sources',
    title: 'Data sources',
    body: (
      <>
        <p>
          Two upstream feeds drive everything we publish.
        </p>
        <p>
          <strong className="text-text-1">SoSoValue ETF flows</strong>{' '}
          — daily inflow and outflow per spot BTC and ETH ETF, captured
          at 04:30 UTC after the prior US trading day has cleared. The
          same source feeds the regime classifier's sector spotlight
          and the news context the AI cites.
        </p>
        <p>
          <strong className="text-text-1">Binance daily klines</strong>{' '}
          — the price source for outcome scoring at each signal's
          validity horizon. Spot price at signal-build time is captured
          via SoSoValue primary with Binance fallback so backfill and
          live scoring use the same series.
        </p>
      </>
    ),
  },
  {
    id: 'detectors',
    title: 'Five detectors',
    body: (
      <>
        <p>
          Each daily cycle runs five independent detectors against the
          latest flow data. Every detector produces a deterministic
          fingerprint so the same hit can't be counted twice across
          re-runs.
        </p>
        <ul className="list-disc pl-5 space-y-1.5">
          <li>
            <strong className="text-text-1">Flow anomaly</strong> — a
            same-sign streak breaks (e.g. ten outflow days followed by
            an inflow day).
          </li>
          <li>
            <strong className="text-text-1">Magnitude</strong> — a
            flow day lands above a trailing-window percentile.
          </li>
          <li>
            <strong className="text-text-1">Acceleration</strong> —
            the second derivative of flow exceeds a stability floor.
          </li>
          <li>
            <strong className="text-text-1">Divergence</strong> —
            institutional vs retail or BTC vs ETH split, gated by a
            magnitude floor so noise doesn't fire.
          </li>
          <li>
            <strong className="text-text-1">Regime shift</strong> — a
            transition in the regime classifier's output (see section
            05).
          </li>
        </ul>
        <p>
          Empirical hit rate per detector is published live on the{' '}
          <Link to="/track-record" className="underline text-text-1">
            track record
          </Link>{' '}
          page, in the "detector precision" card with 95% Wilson
          confidence intervals per cell.
        </p>
      </>
    ),
  },
  {
    id: 'confidence',
    title: 'Confidence + calibration',
    body: (
      <>
        <p>
          Every signal carries a confidence score from 1 to 10, set by
          the AI when it issues the structured analysis. Confidence is
          a CLAIM — what the model says about its own certainty — not
          a measurement.
        </p>
        <p>
          Whether that claim is empirically calibrated (does
          confidence 8 actually win 80% of the time?) is published on
          the same{' '}
          <Link to="/track-record" className="underline text-text-1">
            track record
          </Link>{' '}
          page as a reliability curve, bucketed by confidence and
          split by horizon. Bars are coloured by direction of
          miscalibration: above-diagonal = underclaimed, below =
          overclaimed.
        </p>
        <p>
          Calibration is grouped by AI prompt version because
          confidence calibration is a model-behaviour-per-prompt
          property. A new prompt resets the cohort; old-version
          data becomes a historical baseline.
        </p>
      </>
    ),
  },
  {
    id: 'confirmation',
    title: 'Confirmation score',
    body: (
      <>
        <p>
          After the AI returns its direction, a multi-factor vote
          runs. Each factor independently agrees, disagrees, or
          abstains; the score (0–3) records how many factors agreed
          with the AI's direction.
        </p>
        <ul className="list-disc pl-5 space-y-1.5">
          <li>
            <strong className="text-text-1">Price factor</strong> —
            has price already moved in the AI's direction over the
            last 24 hours by more than a minimum percent floor (1% by
            default).
          </li>
          <li>
            <strong className="text-text-1">Regime factor</strong> —
            does the current regime (MARKUP / ACCUMULATION / MARKDOWN
            / DISTRIBUTION) point the same direction. Regime-shift
            signals abstain here — they ARE the regime signal.
          </li>
          <li>
            <strong className="text-text-1">News factor</strong> —
            reserved for future sentiment scoring; today votes zero
            so the score shape stays stable when it ships.
          </li>
        </ul>
        <p>
          Signals below the delivery minimum (configurable, 1 by
          default) are not pushed to Telegram. The vote is computed
          once at signal-build time and never updated — re-scoring
          later would silently rewrite history.
        </p>
      </>
    ),
  },
  {
    id: 'market-composite',
    title: 'MARKET composite scoring',
    body: (
      <>
        <p>
          Regime-shift signals have no single asset to score against
          — the underlying claim is about the BTC/ETH market as a
          whole. They use a weighted BTC + ETH composite return
          (50% / 50% by default; the two weights must sum to 1.0).
        </p>
        <p>
          A regime-shift signal hits when the signed composite return
          moves in the AI's direction by at least the hit threshold
          (2% by default) over the validity window. Single-asset
          signals continue to score against their own entry / stop /
          target levels — the composite rubric is regime-shift-only.
        </p>
        <p>
          Outcomes scored under the composite rubric carry a distinct{' '}
          <code className="text-[13px] font-mono text-text-1">
            scoring_version
          </code>{' '}
          tag so the two cohorts stay separable in the published
          numbers.
        </p>
      </>
    ),
  },
  {
    id: 'horizon-rubric',
    title: 'Per-horizon outcome rubric',
    body: (
      <>
        <p>
          Each signal carries an explicit validity horizon. Scoring
          matches the horizon — a swing signal isn't penalised for
          failing to hit its target in a six-hour window.
        </p>
        <ul className="list-disc pl-5 space-y-1.5">
          <li>
            <strong className="text-text-1">Scalp (6h)</strong> —
            currently bucketed but unscored; intraday klines are
            pending. Signals exist in the feed but don't contribute
            to hit-rate yet.
          </li>
          <li>
            <strong className="text-text-1">Swing (72h)</strong> —
            the primary scored horizon. Most of the public track
            record reflects this.
          </li>
          <li>
            <strong className="text-text-1">Position (168h)</strong>{' '}
            — scored when the signal's horizon was explicitly set to
            one week.
          </li>
        </ul>
        <p>
          Outcomes are evaluated against the same price source the
          signal was built against (SoSoValue primary, Binance
          fallback) to prevent micro-skew false hits across providers.
        </p>
      </>
    ),
  },
  {
    id: 'what-its-not',
    title: 'What the score is NOT',
    body: (
      <ul className="list-disc pl-5 space-y-2">
        <li>
          <strong className="text-text-1">Not P&amp;L.</strong> Hit
          rate measures direction, not return. The published edge is
          risk-to-reward math (bounded stop, asymmetric target), not
          a high win rate.
        </li>
        <li>
          <strong className="text-text-1">
            Not a guarantee of execution.
          </strong>{' '}
          Suggested entries, stops, and targets describe what the AI
          proposes; whether the user takes the trade and at what
          slippage is the user's decision. The published numbers
          score the SIGNAL, not a backtested portfolio.
        </li>
        <li>
          <strong className="text-text-1">
            Not statistically settled at the current sample.
          </strong>{' '}
          Per-detector breakdowns and per-confidence-bucket
          calibration become meaningful past ~200 evaluated signals.
          Below the sample-size threshold, cells render as "—"
          rather than a noisy point estimate so a small sample
          doesn't masquerade as a precise number.
        </li>
      </ul>
    ),
  },
  {
    id: 'caveats',
    title: 'Caveats',
    body: (
      <>
        <ul className="list-disc pl-5 space-y-2">
          <li>
            Methodology evolves. When the AI prompt's context shape
            changes, the prompt version bumps and old-version data
            becomes a historical baseline, not live reference.
          </li>
          <li>
            We bump the prompt version only on context-shape changes
            (new fields, new scale, new rubric). Small wording edits
            do NOT bump — that would fragment cohorts and stall
            calibration.
          </li>
          <li>
            All timestamps are UTC. All percentages are signed
            fractions. All "hit" verdicts reference the signal's own
            validity window.
          </li>
          <li>
            Nothing here is financial advice. Bound the loss. Let the
            rest compound.
          </li>
        </ul>
        <p className="text-text-3 text-[12px] mt-5">
          Cross-references:{' '}
          <Link to="/track-record" className="underline">
            track record
          </Link>{' '}
          ·{' '}
          <Link to="/signals" className="underline">
            signals feed
          </Link>{' '}
          ·{' '}
          <Link to="/regime" className="underline">
            current regime
          </Link>{' '}
          ·{' '}
          <Link to="/analytics" className="underline">
            analytics breakdowns
          </Link>
        </p>
      </>
    ),
  },
];
