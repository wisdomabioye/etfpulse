/**
 * P2 — pre-submit order economics + cap headroom.
 *
 * Surfaces, inline above the submit button, what the order will cost and
 * whether it will be ACCEPTED:
 *   - Notional (gross), estimated fee (maker on limit / taker on market),
 *     and total cost — via the pure `orderCost` helper.
 *   - Perps funding rate for the asset (informational).
 *   - A cap-headroom warning that PRE-EMPTS the backend's
 *     `*_notional ... would exceed cap` 403 — the exact error the user hit.
 *     Mirrors the gate's reduce-only exemption via `checkOrderAgainstCaps`.
 *
 * Reads the shared account-summary (fee + perps marks) + limits queries —
 * both already fetched elsewhere on the page, so TanStack dedupes. Renders
 * nothing when there's no usable (size, price) pair (e.g. a market order with
 * no live mark), so it never shows a bogus $0.
 */

import { useSpotPrices } from '../../api/queries';
import { useAccountSummary, useExecutionLimits } from '../../hooks/useExecution';
import { formatPrice, formatSignedPct } from '../../lib/format';
import { checkOrderAgainstCaps, orderCost } from '../../lib/positionMath';

interface OrderCostPreviewProps {
  asset: string;
  isPerps: boolean;
  orderType: 'limit' | 'market';
  reduceOnly: boolean;
  /** Raw form strings — parsed here at the boundary. */
  size: string;
  price: string;
}

export function OrderCostPreview({
  asset,
  isPerps,
  orderType,
  reduceOnly,
  size,
  price,
}: OrderCostPreviewProps) {
  const limits = useExecutionLimits(asset);
  const summary = useAccountSummary();
  const spot = useSpotPrices();

  const mark = summary.data?.mark_prices.find((m) => m.asset === asset);
  // Reference price for a MARKET order (which carries no limit price):
  //   perps → the live mark from account-summary;
  //   spot  → the BTC/ETH spot feed (account-summary has no spot mark).
  // Scoped by venue so the displayed source matches the note. A limit order
  // always prices off its own typed limit. NaN when nothing is available →
  // `orderCost` returns null → the preview hides (never a bogus $0).
  const spotRef = asset === 'BTC' ? spot.data?.btc : asset === 'ETH' ? spot.data?.eth : null;
  const marketRef = isPerps ? (mark ? Number(mark.mark_price) : NaN) : (spotRef ?? NaN);
  const nSize = Number(size);
  const refPrice = orderType === 'limit' ? Number(price) : marketRef;

  const fee = summary.data?.fee;
  const feeRate = fee ? Number(orderType === 'limit' ? fee.maker_rate : fee.taker_rate) : 0;
  const cost = orderCost({ size: nSize, price: refPrice, feeRate });

  if (!cost) return null;

  const caps = limits.data
    ? checkOrderAgainstCaps({
        notional: cost.notional,
        reduceOnly,
        dailyCap: Number(limits.data.daily_notional_cap),
        dailyUsed: Number(limits.data.daily_notional_used),
        perSymbolCap: Number(limits.data.per_symbol_cap),
        perSymbolUsed:
          limits.data.per_symbol_used !== null ? Number(limits.data.per_symbol_used) : null,
      })
    : null;
  const over = !!caps && (caps.exceedsDaily || caps.exceedsPerSymbol);
  const funding = isPerps && mark && mark.funding_rate !== null ? Number(mark.funding_rate) : null;

  return (
    <div className="p-3.5 bg-bg-1 border border-line-2 rounded-md space-y-1.5">
      <Row
        label="Notional"
        value={formatPrice(cost.notional)}
        note={orderType === 'market' ? (isPerps ? 'est · live mark' : 'est · spot') : undefined}
      />
      <Row
        label={`Est. fee (${orderType === 'limit' ? 'maker' : 'taker'})`}
        value={feeRate > 0 ? formatPrice(cost.fee) : '—'}
      />
      <Row label="Total" value={formatPrice(cost.total)} strong />
      {funding !== null && (
        <Row label="Funding (perps)" value={formatSignedPct(funding * 100, 4)} muted />
      )}
      {over && caps && (
        <p className="font-mono text-[10.5px] text-loss leading-[1.5] pt-1.5 mt-1 border-t border-line-1">
          {caps.exceedsPerSymbol
            ? `Exceeds the ${asset} 24h cap — only ${formatPrice(caps.perSymbolRemaining ?? 0)} headroom left. The order will be rejected.`
            : `Exceeds your 24h notional cap — only ${formatPrice(caps.dailyRemaining)} headroom left. The order will be rejected.`}
        </p>
      )}
    </div>
  );
}

function Row({
  label,
  value,
  note,
  strong,
  muted,
}: {
  label: string;
  value: string;
  note?: string;
  strong?: boolean;
  muted?: boolean;
}) {
  return (
    <div className="flex justify-between items-baseline">
      <span className="font-mono text-[10px] text-t4 tracking-[0.06em] uppercase">
        {label}
        {note && <span className="text-t4 normal-case tracking-normal"> · {note}</span>}
      </span>
      <span
        className={`font-mono tabular-nums text-[12px] ${
          strong ? 'text-t1 font-semibold' : muted ? 'text-t3' : 'text-t2'
        }`}
      >
        {value}
      </span>
    </div>
  );
}
