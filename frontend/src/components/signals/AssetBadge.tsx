import type { AssetSymbol } from '../../api/types';

interface AssetBadgeProps {
  asset: AssetSymbol;
  size?: 'sm' | 'md';
}

/**
 * Solid-color asset pill. BTC → orange, ETH → blue, with dark text.
 *
 * MARKET (cross-asset sentinel, PR F.3) renders as a neutral OUTLINED
 * chip — transparent bg, border-2, text-text-2 — so users can tell at
 * a glance "this isn't a single-asset call, it's market-wide regime
 * intelligence." Reusing the BTC or ETH color here would falsely imply
 * the signal is directional on one asset.
 */
export function AssetBadge({ asset, size = 'md' }: AssetBadgeProps) {
  const paddingClass = size === 'sm' ? 'px-[7px] py-[2px] text-[10px]' : 'px-[9px] py-[3px] text-[11px]';
  const radiusClass = size === 'sm' ? 'rounded-[4px]' : 'rounded-[5px]';

  if (asset === 'MARKET') {
    return (
      <span
        className={`inline-flex items-center font-mono font-semibold tracking-[0.04em] border border-border-3 bg-transparent text-text-2 ${paddingClass} ${radiusClass}`}
      >
        {asset}
      </span>
    );
  }

  const bg = asset === 'BTC' ? 'var(--color-btc)' : 'var(--color-eth)';
  return (
    <span
      className={`inline-flex items-center font-mono font-semibold tracking-[0.04em] ${paddingClass} ${radiusClass}`}
      style={{ background: bg, color: '#0a0a0b' }}
    >
      {asset}
    </span>
  );
}
