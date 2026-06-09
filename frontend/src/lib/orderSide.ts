import type { ColorToken } from './colorMix';

export interface SideDisplay {
  /** "Long" | "Short" on perps; "Buy" | "Sell" on spot. */
  label: string;
  /** Directional arrow on perps ("▲"/"▼"); empty on spot (no direction). */
  glyph: string;
  /** `--win` for the positive side, `--loss` for the negative. */
  token: ColorToken;
  positive: boolean;
}

/**
 * Venue-aware side label.
 *
 * Perps are DIRECTIONAL, leveraged positions → Long / Short (with an
 * up/down arrow). Spot is a plain buy/sell of a holding — there is no
 * "short" on spot — so it uses Buy / Sell and no directional arrow.
 * Showing "Long/Short" on a spot row is misleading; this keeps each
 * venue's vocabulary distinct.
 *
 * Accepts either the order-level side (`buy`/`sell`) or the
 * position-level side (`long`/`short`); both map to a positive (buy/long)
 * or negative (sell/short) direction.
 */
export function sideDisplay(venue: string, side: string): SideDisplay {
  const positive = side === 'buy' || side === 'long';
  const perps = venue === 'sodex_perps';
  return {
    positive,
    token: positive ? '--win' : '--loss',
    glyph: perps ? (positive ? '▲' : '▼') : '',
    label: perps ? (positive ? 'Long' : 'Short') : positive ? 'Buy' : 'Sell',
  };
}
