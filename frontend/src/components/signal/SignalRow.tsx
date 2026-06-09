import type { KeyboardEvent } from 'react';

import type { SignalListItem } from '../../api/types';
import { DETECTORS } from '../../lib/constants';
import { detectorColorToken } from '../../lib/colors';
import { cssVar } from '../../lib/colorMix';
import { formatAgo, isWithin } from '../../lib/format';
import { ActionTag, AssetBadge, ConfidenceBadge, ConfirmationPips, DetectorIcon } from '../ui';

interface SignalRowProps {
  signal: SignalListItem;
  onClick?: () => void;
  /** Active (selected) row — amber border. */
  active?: boolean;
}

const RECENT_MS = 60 * 60 * 1000; // < 1h → "fresh" highlight

/**
 * Full feed row — one signal identity reused across the Signals list. Ported
 * from the prototype's `SignalRow`, mapped to the real `SignalListItem` with
 * null-handling the prototype lacked (AI-failed signals carry null
 * action/confidence/confirmation/headline). The left border keeps the
 * detector's identity color; the rest lifts to amber on hover (CSS, not JS
 * state). Freshness-only right column — per-row outcomes live on TrackRecord.
 */
export function SignalRow({ signal, onClick, active = false }: SignalRowProps) {
  const det = DETECTORS[signal.signal_type];
  const edge = active ? cssVar('--acc') : cssVar(detectorColorToken(signal.signal_type));
  const recent = isWithin(signal.created_at, RECENT_MS);

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
      className={`grid items-center gap-[var(--gap)] px-4 py-3.5 bg-bg-2 cursor-pointer rounded-md border transition-[border-color,transform] duration-[var(--dur-1)] ease-[var(--ease)] ${
        active
          ? 'border-acc'
          : 'border-line-2 hover:border-acc-line hover:-translate-y-px'
      }`}
      style={{
        gridTemplateColumns: 'auto auto 1fr auto auto auto',
        borderLeftWidth: 3,
        borderLeftColor: edge,
      }}
    >
      {/* asset + detector */}
      <div className="flex items-center gap-2 min-w-0">
        <AssetBadge asset={signal.asset} size="sm" />
        <DetectorIcon type={signal.signal_type} size={14} />
      </div>

      {/* action */}
      {signal.suggested_action ? (
        <ActionTag action={signal.suggested_action} size="sm" />
      ) : (
        <span className="font-mono text-[10px] text-t4">—</span>
      )}

      {/* headline + meta */}
      <div className="min-w-0">
        <div className="text-[13.5px] font-medium text-t1 truncate tracking-[-0.005em]">
          {signal.headline ?? 'AI analysis pending'}
        </div>
        <div className="font-mono text-[10.5px] text-t4 mt-[3px] flex gap-2">
          <span style={{ color: cssVar(detectorColorToken(signal.signal_type)) }}>{det.label}</span>
          <span>·</span>
          <span>#{signal.id}</span>
          {signal.time_horizon && (
            <>
              <span>·</span>
              <span className="capitalize">{signal.time_horizon}</span>
            </>
          )}
        </div>
      </div>

      {/* confirmation */}
      <div className="hidden sm:flex flex-col items-center gap-[3px]">
        {signal.confirmation_score != null ? (
          <>
            <ConfirmationPips value={signal.confirmation_score} />
            <span className="font-mono text-[9px] text-t4">conf {signal.confirmation_score}/3</span>
          </>
        ) : (
          <span className="font-mono text-[9px] text-t4">—</span>
        )}
      </div>

      {/* freshness */}
      <div className="hidden sm:block text-right min-w-[70px]">
        <span className={`font-mono text-[11px] ${recent ? 'text-acc-hi' : 'text-t3'}`}>
          {formatAgo(signal.created_at)}
        </span>
      </div>

      {/* confidence */}
      {signal.confidence != null ? (
        <ConfidenceBadge value={signal.confidence} />
      ) : (
        <span className="font-mono text-[11px] text-t4">—</span>
      )}
    </div>
  );
}
