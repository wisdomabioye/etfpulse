/**
 * Banner shown above the trade grid when the user landed via `?signal_id=N`
 * (from the SignalDetail "Execute this signal" CTA or a Telegram alert). Four
 * states: loading / error / loaded-non-executable / loaded-executable.
 */

import { Link } from 'react-router-dom';

import type { SignalDetail } from '../../api/types';
import { isExecutableSignal } from '../../lib/signalExecute';
import { ActionTag, AssetBadge, DetectorBadge } from '../ui';

export function SignalPrefillBanner({
  signalId,
  signal,
  isLoading,
  isError,
}: {
  signalId: number;
  signal: SignalDetail | undefined;
  isLoading: boolean;
  isError: boolean;
}) {
  const linkable = `/signals/${signalId}`;
  if (isLoading) {
    return (
      <div className="text-[12px] text-t3 border border-line-2 bg-bg-2 rounded-md px-3 py-2">
        Loading signal #{signalId}…
      </div>
    );
  }
  if (isError || !signal) {
    return (
      <div className="text-[12px] text-amber-200 border border-amber-500/30 bg-amber-500/5 rounded-md px-3 py-2">
        Signal #{signalId} could not be loaded. Form will submit with this id attached, but
        no prefill was applied — review the fields before signing.
      </div>
    );
  }
  if (!isExecutableSignal(signal)) {
    return (
      <div className="text-[12px] text-amber-200 border border-amber-500/30 bg-amber-500/5 rounded-md px-3 py-2">
        Signal #{signalId} isn&apos;t actionable (asset {signal.asset} / direction{' '}
        {signal.ai_analysis?.suggested_action ?? '—'}). Form is NOT prefilled.
      </div>
    );
  }
  const action = signal.ai_analysis!.suggested_action;
  return (
    <div className="flex items-center gap-3.5 px-4 py-3 bg-acc-soft border border-acc-line rounded-md flex-wrap">
      <span className="font-mono text-[10px] text-acc-hi tracking-[0.1em] uppercase">
        Prefilled from
      </span>
      <AssetBadge asset={signal.asset} size="sm" />
      <DetectorBadge type={signal.signal_type} size="sm" />
      <ActionTag action={action} size="sm" />
      <span className="text-[12.5px] text-t2 truncate flex-1 min-w-[120px]">
        {signal.ai_analysis!.headline}
      </span>
      <Link to={linkable} className="font-mono text-[11px] text-acc-hi whitespace-nowrap">
        view signal →
      </Link>
    </div>
  );
}
