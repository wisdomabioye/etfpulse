import type { AssetSymbol } from '../../api/types';

interface AssetBadgeProps {
  asset: AssetSymbol;
  size?: 'sm' | 'md';
}

/**
 * Solid-color asset pill. BTC → orange, ETH → blue, with dark text.
 *
 * Matches mock primitives.jsx AssetBadge — mono font, tabular numbers, small
 * padding. Dark text on colored bg contrasts well on both brand colors.
 */
export function AssetBadge({ asset, size = 'md' }: AssetBadgeProps) {
  const bg = asset === 'BTC' ? 'var(--color-btc)' : 'var(--color-eth)';
  const paddingClass = size === 'sm' ? 'px-[7px] py-[2px] text-[10px]' : 'px-[9px] py-[3px] text-[11px]';
  const radiusClass = size === 'sm' ? 'rounded-[4px]' : 'rounded-[5px]';

  return (
    <span
      className={`inline-flex items-center font-mono font-semibold tracking-[0.04em] ${paddingClass} ${radiusClass}`}
      style={{ background: bg, color: '#0a0a0b' }}
    >
      {asset}
    </span>
  );
}
