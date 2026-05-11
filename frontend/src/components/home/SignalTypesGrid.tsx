/**
 * 5-cell explainer grid for the five detector types — Flow Anomaly,
 * Magnitude, Acceleration, Divergence, Regime Shift.
 *
 * First-time visitors land on Home with no idea what they'd be subscribing
 * to. The `HowItWorks` strip below answers "how"; this answers "what" —
 * one tagline per detector, no detector math, no thresholds. The detail
 * lives on `/signals` filtered views and individual signal pages.
 *
 * Layout reuses the same hairline-grid trick as HowItWorks (1px gap on the
 * outer grid with border-2 background; inner cells have bg-2 so the gap
 * shows through as the divider). Responsive: 1 col mobile, 2 cols tablet,
 * 3 cols desktop — 5 cells with `auto-rows-fr` keeps row heights equal
 * regardless of which line of copy is longest.
 *
 * Copy intent:
 *   - One sentence each. ~12-18 words.
 *   - "What pattern did we see" not "what threshold did we cross".
 *   - Lay-trader voice, not detector-paper voice.
 */

import type { SignalType } from '../../api/types';
import { formatSignalType } from '../../lib/format';

interface TypeEntry {
  type: SignalType;
  /** Short, lay-trader-voice description of what the detector watches for. */
  body: string;
}

const TYPES: TypeEntry[] = [
  {
    type: 'flow_anomaly',
    body: 'A multi-day streak of one-way ETF flows breaks. Sustained buying or selling has reversed.',
  },
  {
    type: 'magnitude',
    body: 'A single day of flows lands in the top percentile of recent history — an unusually large print.',
  },
  {
    type: 'acceleration',
    body: 'The pace of net flow has shifted sharply between the prior window and the most recent days.',
  },
  {
    type: 'divergence',
    body: 'Institutional and retail flows are pulling apart — or BTC and ETH have split direction.',
  },
  {
    type: 'regime_shift',
    body: 'The composite market regime (flows · news · macro) crossed into a new Wyckoff-style phase.',
  },
];

export function SignalTypesGrid() {
  return (
    <div
      className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-px rounded-lg overflow-hidden border border-border-2 auto-rows-fr"
      style={{ background: 'var(--color-border-2)' }}
    >
      {TYPES.map((entry, i) => (
        <SignalTypeCard key={entry.type} n={i + 1} type={entry.type} body={entry.body} />
      ))}
    </div>
  );
}

interface SignalTypeCardProps {
  /** 1-based index for the small mono numeral in the corner. */
  n: number;
  type: SignalType;
  body: string;
}

/** One detector explainer cell. Exported so future surfaces (an `/about`
 *  page, a settings tooltip, a sales deck) can drop a single cell without
 *  rebuilding the grid. */
export function SignalTypeCard({ n, type, body }: SignalTypeCardProps) {
  return (
    <div className="bg-bg-2 px-6 py-[22px]">
      <div className="font-mono text-[11px] text-accent tracking-[0.1em] mb-3.5">
        {String(n).padStart(2, '0')} / {type.toUpperCase().replace('_', ' ')}
      </div>
      <div
        className="text-[18px] font-semibold text-text-1 mb-2"
        style={{ letterSpacing: '-0.01em' }}
      >
        {formatSignalType(type)}
      </div>
      <div className="text-[13px] text-text-2 leading-[1.55]">{body}</div>
    </div>
  );
}
