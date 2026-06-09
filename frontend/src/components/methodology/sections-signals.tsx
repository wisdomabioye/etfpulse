import { Link } from 'react-router-dom';

import type { SectionContent } from './sectionTypes';

/**
 * Methodology page content — signal-side sections (data sources
 * through the MARKET composite rubric). Pure data, no UI components
 * defined here. Body slots return ReactNode so internal Links +
 * emphasis markup live next to the prose that uses them.
 *
 * Every factual claim is grounded in CLAUDE.md (the internal
 * codebase guide). When config defaults or behavior change there,
 * update the matching line here in the same change set to keep
 * methodology aligned with what production actually does.
 */

export const SIGNAL_SECTIONS: SectionContent[] = [
  {
    id: 'data-sources',
    title: 'Data sources',
    body: (
      <>
        <p>
          Two upstream feeds drive everything we publish.
        </p>
        <p>
          <strong className="text-t1">SoSoValue ETF flows</strong>{' '}
          — daily inflow and outflow per spot BTC and ETH ETF, captured
          at 04:30 UTC after the prior US trading day has cleared. The
          same source feeds the regime classifier's sector spotlight
          and the news context the AI cites.
        </p>
        <p>
          <strong className="text-t1">Binance daily klines</strong>{' '}
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
            <strong className="text-t1">Flow anomaly</strong> — a
            same-sign streak breaks (e.g. ten outflow days followed by
            an inflow day).
          </li>
          <li>
            <strong className="text-t1">Magnitude</strong> — a
            flow day lands above a trailing-window percentile.
          </li>
          <li>
            <strong className="text-t1">Acceleration</strong> —
            the second derivative of flow exceeds a stability floor.
          </li>
          <li>
            <strong className="text-t1">Divergence</strong> —
            institutional vs retail or BTC vs ETH split, gated by a
            magnitude floor so noise doesn't fire.
          </li>
          <li>
            <strong className="text-t1">Regime shift</strong> — a
            transition in the regime classifier's output (see section
            05).
          </li>
        </ul>
        <p>
          Empirical hit rate per detector is published live on the{' '}
          <Link to="/track-record" className="underline text-t1">
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
          <Link to="/track-record" className="underline text-t1">
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
            <strong className="text-t1">Price factor</strong> —
            has price already moved in the AI's direction over the
            last 24 hours by more than a minimum percent floor (1% by
            default).
          </li>
          <li>
            <strong className="text-t1">Regime factor</strong> —
            does the current regime (MARKUP / ACCUMULATION / MARKDOWN
            / DISTRIBUTION) point the same direction. Regime-shift
            signals abstain here — they ARE the regime signal.
          </li>
          <li>
            <strong className="text-t1">News factor</strong> —
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
          <code className="text-[13px] font-mono text-t1">
            scoring_version
          </code>{' '}
          tag so the two cohorts stay separable in the published
          numbers.
        </p>
      </>
    ),
  },
];
