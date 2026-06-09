import type { KeyboardEvent } from 'react';

import type { SignalListItem } from '../../api/types';
import { detectorColorToken } from '../../lib/colors';
import { cssVar } from '../../lib/colorMix';
import { formatAgo } from '../../lib/format';
import { ActionTag, AssetBadge, ConfidenceBadge, DetectorBadge } from '../ui';

interface SignalCardMiniProps {
  signal: SignalListItem;
  onClick?: () => void;
}

/**
 * Compact signal card — used on Home "most recent" + pulse contexts. Ported
 * from the prototype's `SignalCardMini`, mapped to `SignalListItem` with
 * null-safe action/confidence/headline. The detector-color left border stays
 * fixed; the rest of the border lifts to amber on hover (CSS).
 */
export function SignalCardMini({ signal, onClick }: SignalCardMiniProps) {
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onClick?.();
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={onKeyDown}
      className="px-[15px] py-3.5 bg-bg-2 border border-line-2 rounded-md cursor-pointer transition-colors duration-[var(--dur-1)] hover:border-acc-line"
      style={{ borderLeftWidth: 3, borderLeftColor: cssVar(detectorColorToken(signal.signal_type)) }}
    >
      <div className="flex items-center justify-between mb-2.5">
        <div className="flex gap-[7px] items-center">
          <AssetBadge asset={signal.asset} size="sm" />
          <DetectorBadge type={signal.signal_type} size="sm" />
        </div>
        {signal.confidence != null ? (
          <ConfidenceBadge value={signal.confidence} />
        ) : (
          <span className="font-mono text-[11px] text-t4">—</span>
        )}
      </div>
      <div className="text-[14px] font-medium leading-[1.4] tracking-[-0.01em] mb-3 text-pretty">
        {signal.headline ?? 'AI analysis pending'}
      </div>
      <div className="flex items-center justify-between pt-2.5 border-t border-line-1">
        {signal.suggested_action ? (
          <ActionTag action={signal.suggested_action} size="sm" />
        ) : (
          <span className="font-mono text-[10px] text-t4">—</span>
        )}
        <span className="font-mono text-[10.5px] text-t4">{formatAgo(signal.created_at)}</span>
      </div>
    </div>
  );
}
