import { Link } from 'react-router-dom';

import type { SectionContent } from './sectionTypes';

/**
 * Methodology page content — scoring-side sections (per-horizon
 * rubric, what the score is NOT, and caveats). Pure data, no UI
 * components defined here. Body slots return ReactNode so internal
 * Links + emphasis markup live next to the prose that uses them.
 *
 * Every factual claim is grounded in CLAUDE.md (the internal
 * codebase guide). When config defaults or behavior change there,
 * update the matching line here in the same change set to keep
 * methodology aligned with what production actually does.
 */

export const SCORING_SECTIONS: SectionContent[] = [
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
            <strong className="text-t1">Scalp (6h)</strong> —
            currently bucketed but unscored; intraday klines are
            pending. Signals exist in the feed but don't contribute
            to hit-rate yet.
          </li>
          <li>
            <strong className="text-t1">Swing (72h)</strong> —
            the primary scored horizon. Most of the public track
            record reflects this.
          </li>
          <li>
            <strong className="text-t1">Position (168h)</strong>{' '}
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
          <strong className="text-t1">Not P&amp;L.</strong> Hit
          rate measures direction, not return. The published edge is
          risk-to-reward math (bounded stop, asymmetric target), not
          a high win rate.
        </li>
        <li>
          <strong className="text-t1">
            Not a guarantee of execution.
          </strong>{' '}
          Suggested entries, stops, and targets describe what the AI
          proposes; whether the user takes the trade and at what
          slippage is the user's decision. The published numbers
          score the SIGNAL, not a backtested portfolio.
        </li>
        <li>
          <strong className="text-t1">
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
        <p className="text-t3 text-[12px] mt-5">
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
