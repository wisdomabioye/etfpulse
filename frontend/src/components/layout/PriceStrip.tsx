import { useSpotPrices } from '../../api/queries';
import { AssetBadge } from '../ui';

/**
 * Live BTC + ETH spot price strip in the TopNav status row. Ported to the
 * prototype's treatment: a colored coin `AssetBadge` + the price per asset,
 * with a hairline divider between them (no fake direction arrow — the spot
 * feed carries no per-tick direction).
 *
 * Visibility rules:
 *   - Loading: dim badge + "—" placeholder.
 *   - Both prices null (provider outage) OR error: the strip hides itself.
 *   - One asset null: the surviving asset still renders.
 *
 * Format: `$83,142` (no decimals) — mobile-skim density.
 */
export function PriceStrip() {
  const { data, isLoading, isError } = useSpotPrices();

  if (isLoading) {
    return (
      <span className="inline-flex items-center gap-3 select-none">
        <PriceItem asset="BTC" price={null} />
        <span className="w-px h-3.5 bg-line-2" aria-hidden />
        <PriceItem asset="ETH" price={null} />
      </span>
    );
  }

  if (isError || !data) return null;
  if (data.btc === null && data.eth === null) return null;

  // Provenance hint when the source isn't the happy-path primary.
  const showSource = data.source !== null && data.source !== 'sosovalue';

  return (
    <span
      className="inline-flex items-center gap-3 select-none"
      title={data.source ? `Source: ${data.source}` : undefined}
      aria-label={`Live spot prices${data.source ? ` (source: ${data.source})` : ''}`}
    >
      {data.btc !== null && <PriceItem asset="BTC" price={data.btc} />}
      {data.btc !== null && data.eth !== null && <span className="w-px h-3.5 bg-line-2" aria-hidden />}
      {data.eth !== null && <PriceItem asset="ETH" price={data.eth} />}
      {showSource && (
        <span className="font-mono text-[10px] text-warn uppercase tracking-[0.08em]" aria-hidden>
          · {data.source}
        </span>
      )}
    </span>
  );
}

function PriceItem({ asset, price }: { asset: 'BTC' | 'ETH'; price: number | null }) {
  return (
    <span className="inline-flex items-center gap-2">
      <AssetBadge asset={asset} size="sm" />
      <span
        className={`font-mono tabular-nums text-[12px] font-semibold ${price === null ? 'text-t4' : 'text-t1'}`}
      >
        {price === null ? '—' : `$${formatCompactUsd(price)}`}
      </span>
    </span>
  );
}

/** No decimals, comma thousands — the nav strip's tight density. */
function formatCompactUsd(n: number): string {
  return Math.round(n).toLocaleString('en-US');
}
