import { assetBrandToken } from '../../lib/colors';
import { cssVar } from '../../lib/colorMix';

interface AssetBadgeProps {
  /** Accepts the bare detector symbols (`BTC`/`ETH`/`MARKET`) AND the
   *  venue-symbol forms execution orders carry (`BTC-USD`, `ETH/USDT`). */
  asset: string;
  size?: 'sm' | 'md';
}

/**
 * Solid asset chip — ported from the prototype's `AssetBadge`. Known assets
 * (incl. `BASE-QUOTE` forms resolved to their base) render in the asset's
 * brand color with near-black `--ink` text. An unrecognised ticker can't use
 * `--ink` (no bright fill → dark-on-dark), so it falls back to a neutral,
 * always-readable chip (`bg-3` surface, `t1` text, `line-3` border).
 */
export function AssetBadge({ asset, size = 'md' }: AssetBadgeProps) {
  const sizeCls = size === 'sm' ? 'px-1.5 py-px text-[10px]' : 'px-2 py-0.5 text-[11px]';
  const base = `inline-flex items-center font-mono font-semibold tracking-[0.03em] rounded-sm ${sizeCls}`;
  const token = assetBrandToken(asset);

  if (token === null) {
    return <span className={`${base} bg-bg-3 text-t1 border border-line-3`}>{asset}</span>;
  }
  return (
    <span className={base} style={{ background: cssVar(token), color: 'var(--ink)' }}>
      {asset}
    </span>
  );
}
