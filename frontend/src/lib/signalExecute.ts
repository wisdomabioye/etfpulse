/**
 * Signal → trade-execution bridge helpers (SIG2X).
 *
 * Pure functions, no React. The same gating logic is needed in three
 * places — SignalDetail (CTA visibility), Execute (prefill resolution),
 * and the corresponding Telegram-side gate on the backend
 * (`pipeline/delivery.py:build_signal_keyboard`). Keeping the FE rules
 * in one module means SignalDetail and Execute can never disagree on
 * "is this signal executable".
 *
 * The Telegram-side gate lives in Python and is INDEPENDENTLY enforced
 * — it duplicates the rules below by intent (different language,
 * different render context). The contract pinning both is documented
 * in CLAUDE.md "SIG2X" section.
 */

import type { Side, Venue } from '../api/execution';
import type { AIAnalysis, AssetSymbol, SuggestedAction } from '../api/types';
import { ASSETS } from './constants';

/** Assets we actually wire to a venue. Derived from the canonical
 *  `ASSETS` tuple minus the `MARKET` sentinel — regime-shift signals
 *  describe BTC/ETH market-wide moves, not single-asset trade calls,
 *  so surfacing an Execute CTA on them would force the user to pick
 *  an asset manually and muddy the signal's meaning. Driving this
 *  from `ASSETS` means a future asset addition (e.g. SOL) lands here
 *  for free without a manual edit. */
const TRADEABLE_ASSETS = new Set<AssetSymbol>(
  ASSETS.filter((a): a is Exclude<AssetSymbol, 'MARKET'> => a !== 'MARKET'),
);

/** AI-emitted directions that map to a concrete trade. `'wait'`
 *  intentionally hides the CTA — there's nothing to execute. */
const ACTIONABLE_DIRECTIONS = new Set<SuggestedAction>([
  'consider long',
  'consider short',
]);

/** Subset of `SignalDetail` we need for the gate. Kept narrow so this
 *  helper doesn't drag in the full type and so test fixtures stay
 *  small. */
interface ExecutableInputs {
  asset: AssetSymbol;
  ai_analysis: Pick<AIAnalysis, 'suggested_action'> | null;
}

/**
 * Should the FE render an "Execute this signal" CTA for this signal?
 *
 * True iff:
 *   - The asset is tradeable (BTC or ETH — NOT MARKET).
 *   - The AI analysis exists (no NULL-confidence signal).
 *   - The AI-emitted direction is actionable (not `'wait'`).
 *
 * Mirrored on the backend in `pipeline/delivery.py:build_signal_keyboard`
 * — the Telegram alert keyboard adds the "⚡ Execute" button only
 * when this same predicate is satisfied.
 */
export function isExecutableSignal(signal: ExecutableInputs): boolean {
  if (!TRADEABLE_ASSETS.has(signal.asset)) return false;
  if (signal.ai_analysis == null) return false;
  return ACTIONABLE_DIRECTIONS.has(signal.ai_analysis.suggested_action);
}

/**
 * Build the deep-link the CTA should navigate to. Single source of
 * truth for the URL shape so a future query-param addition (e.g.
 * `&from=telegram`) lands in one place.
 */
export function signalExecuteHref(signalId: number): string {
  return `/execute?signal_id=${signalId}`;
}

/** Side that maps to the AI's directional call. Long → buy, short →
 *  sell. `'wait'` returns null — the caller should never reach here
 *  if `isExecutableSignal` is gating, but the null preserves type
 *  safety against drift. Return type is the canonical `Side` from
 *  `api/execution` (not a re-declared literal) so a future Side
 *  expansion (e.g. adding `'short_close'`) propagates in one place. */
export function sideForSuggestedAction(action: SuggestedAction): Side | null {
  if (action === 'consider long') return 'buy';
  if (action === 'consider short') return 'sell';
  return null;
}

/** Default venue per direction. Short orders MUST go to perps (spot
 *  can't short); long defaults to spot but the user can switch to
 *  perps in the form. Return type is the canonical `Venue` from
 *  `api/execution` so a venue addition propagates here for free. */
export function defaultVenueForSuggestedAction(
  action: SuggestedAction,
): Venue | null {
  if (action === 'consider long') return 'sodex_spot';
  if (action === 'consider short') return 'sodex_perps';
  return null;
}
