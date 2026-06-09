/**
 * Pure execution math — order notional, unrealized PnL, and order cost.
 *
 * Shared by the positions table (uPnL), the order form (cost preview +
 * cap headroom), and the limits card. Kept pure (numbers in, numbers out)
 * so it's trivially testable and reusable; callers parse the API's decimal
 * STRINGS into numbers at the boundary.
 */

/** Gross notional of an order leg = |size| × price. */
export function orderNotional(size: number, price: number): number {
  return Math.abs(size) * price;
}

export interface Pnl {
  /** Absolute PnL in quote currency (USD). */
  pnl: number;
  /** PnL as a percent of entry notional (signed). */
  pnlPct: number;
}

/**
 * Unrealized PnL of an open position against a live mark.
 *
 * `side` accepts the position form (`long`/`short`) or the order form
 * (`buy`/`sell`) — buy/long are positive-direction. Returns null when the
 * inputs can't yield a meaningful number (missing/zero entry or size, or a
 * non-finite mark) so the UI renders "—" rather than NaN.
 */
export function unrealizedPnl(args: {
  side: string;
  size: number;
  entry: number;
  mark: number;
}): Pnl | null {
  const { side, size, entry, mark } = args;
  if (
    !Number.isFinite(size) ||
    !Number.isFinite(entry) ||
    !Number.isFinite(mark) ||
    entry <= 0 ||
    size <= 0
  ) {
    return null;
  }
  const dir = side === 'long' || side === 'buy' ? 1 : -1;
  const pnl = (mark - entry) * size * dir;
  const pnlPct = ((mark - entry) / entry) * 100 * dir;
  return { pnl, pnlPct };
}

export interface OrderCost {
  /** |size| × price. */
  notional: number;
  /** notional × feeRate (0 when no/invalid fee rate). */
  fee: number;
  /** notional + fee. */
  total: number;
}

/**
 * Cost of a would-be order: notional + estimated fee. `feeRate` is a
 * fraction (e.g. 0.0005 = 5 bps). Returns null when size/price are invalid
 * so the preview hides rather than showing a bogus $0.
 */
export function orderCost(args: {
  size: number;
  price: number;
  feeRate: number;
}): OrderCost | null {
  const { size, price, feeRate } = args;
  if (!Number.isFinite(size) || !Number.isFinite(price) || size <= 0 || price <= 0) {
    return null;
  }
  const notional = orderNotional(size, price);
  const fee = Number.isFinite(feeRate) && feeRate > 0 ? notional * feeRate : 0;
  return { notional, fee, total: notional + fee };
}

export interface CapCheck {
  /** Order notional would push 24h rolling usage past the account cap. */
  exceedsDaily: boolean;
  /** Order notional would push per-symbol 24h usage past the symbol cap. */
  exceedsPerSymbol: boolean;
  /** Remaining 24h notional headroom (cap − used), floored at 0. */
  dailyRemaining: number;
  /** Remaining per-symbol headroom (cap − used), floored at 0; null when the
   *  caller didn't scope to an asset (per-symbol usage unknown). */
  perSymbolRemaining: number | null;
}

/**
 * Pre-flight check of a would-be order against the user's notional caps —
 * pre-empts the backend's `*_notional ... would exceed cap` 403.
 *
 * Mirrors the backend risk gate's CAP-EXEMPT rule: a `reduceOnly` order can
 * only reduce exposure, so it bypasses BOTH notional caps (the gateway/risk
 * gate never evaluates them). We surface the same exemption here so the UI
 * doesn't warn about a cap that won't actually be enforced.
 */
export function checkOrderAgainstCaps(args: {
  notional: number;
  reduceOnly: boolean;
  dailyCap: number;
  dailyUsed: number;
  perSymbolCap: number;
  perSymbolUsed: number | null;
}): CapCheck {
  const { notional, reduceOnly, dailyCap, dailyUsed, perSymbolCap, perSymbolUsed } = args;
  const dailyRemaining = Math.max(0, dailyCap - dailyUsed);
  const perSymbolRemaining =
    perSymbolUsed === null ? null : Math.max(0, perSymbolCap - perSymbolUsed);
  if (reduceOnly || !Number.isFinite(notional) || notional <= 0) {
    return { exceedsDaily: false, exceedsPerSymbol: false, dailyRemaining, perSymbolRemaining };
  }
  return {
    exceedsDaily: dailyUsed + notional > dailyCap,
    exceedsPerSymbol: perSymbolRemaining !== null && perSymbolUsed! + notional > perSymbolCap,
    dailyRemaining,
    perSymbolRemaining,
  };
}
