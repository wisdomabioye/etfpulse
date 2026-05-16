import { useSpotPrices } from '../../api/queries';

/**
 * Live BTC + ETH spot price strip, pinned to the right edge of the TopNav
 * (next to `LivePulse`). One backend call serves both prices via the
 * `/api/prices/spot` cache; this hook refetches every 60s so the strip
 * stays fresh enough for a glance without putting load on SoSoValue.
 *
 * Visibility rules:
 *   - Loading (first paint): subtle dash placeholder — no shimmer, this is
 *     a 12px strip, a skeleton would draw more attention than the value.
 *   - Both prices null (provider outage) OR query error: the strip hides
 *     itself entirely. It's a nice-to-have surface; failing closed beats
 *     a "$—" eyesore.
 *   - One asset null: the surviving asset still renders.
 *
 * Format choice: `$83,142` (no decimals). Mobile-skim density. The detail
 * pages render full precision; the nav strip is for orientation.
 */
export function PriceStrip() {
  const { data, isLoading, isError } = useSpotPrices();

  if (isLoading) {
    return (
      <span className="inline-flex items-center gap-2 font-mono text-[12px] text-text-4 select-none">
        <span>BTC —</span>
        <span className="text-text-4">·</span>
        <span>ETH —</span>
      </span>
    );
  }

  if (isError || !data) return null;
  if (data.btc === null && data.eth === null) return null;

  // Show a small provenance label when source is anything other than the
  // happy-path primary (sosovalue). "binance" means SoSoValue failed and we
  // fell over; "mixed" means one asset came from each. Both are operationally
  // interesting and easy to miss in a tooltip-only surface.
  const showSource = data.source !== null && data.source !== 'sosovalue';

  return (
    <span
      className="inline-flex items-center gap-2 font-mono text-[12px] text-text-2 select-none tabular-nums"
      title={data.source ? `Source: ${data.source}` : undefined}
      aria-label={`Live spot prices${data.source ? ` (source: ${data.source})` : ''}`}
    >
      {data.btc !== null && <span>BTC ${formatCompactUsd(data.btc)}</span>}
      {data.btc !== null && data.eth !== null && (
        <span className="text-text-4">·</span>
      )}
      {data.eth !== null && <span>ETH ${formatCompactUsd(data.eth)}</span>}
      {showSource && (
        <span
          className="text-[10px] text-warn uppercase tracking-[0.08em]"
          aria-hidden
        >
          · {data.source}
        </span>
      )}
    </span>
  );
}

/** No decimals, comma thousands. Frontend `lib/format.formatUsdPrice` gives
 *  2 decimals — keep this local for the nav strip's tighter density. */
function formatCompactUsd(n: number): string {
  return Math.round(n).toLocaleString('en-US');
}
