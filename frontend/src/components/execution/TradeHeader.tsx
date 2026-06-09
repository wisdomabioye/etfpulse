/**
 * Execute page header — title + non-custodial subtitle + paper/live mode pill,
 * plus the request-live breadcrumb for paper users.
 */

import type { WalletMeResponse } from '../../api/execution';
import type { ColorToken } from '../../lib/colorMix';
import { colorMix, cssVar } from '../../lib/colorMix';
import { RequestLiveBlock } from './RequestLiveBlock';

export function TradeHeader({ me }: { me: WalletMeResponse }) {
  return (
    <div>
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-[26px] font-semibold tracking-[-0.02em]">Execute</h1>
          <p className="text-t3 text-[13px] mt-1.5">
            You sign every action in your wallet. We never hold keys or auto-trade.
          </p>
        </div>
        <ModePill paper={me.paper_trade} />
      </div>
      {/* Paper users get the request-live breadcrumb (#185). */}
      {me.paper_trade && (
        <div className="mt-4">
          <RequestLiveBlock />
        </div>
      )}
    </div>
  );
}

export function ModePill({ paper }: { paper: boolean }) {
  const token: ColorToken = paper ? '--warn' : '--win';
  return (
    <span
      className="font-mono text-[11px] px-[11px] py-[5px] rounded-sm whitespace-nowrap"
      style={{
        background: colorMix(token, 14, cssVar('--bg-2')),
        color: cssVar(token),
        border: `1px solid ${colorMix(token, 30)}`,
      }}
    >
      {paper ? '◐ PAPER MODE' : '● LIVE TRADING'}
    </span>
  );
}
