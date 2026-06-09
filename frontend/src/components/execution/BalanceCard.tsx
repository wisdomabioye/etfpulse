/**
 * Spot wallet balances (P1). Reads the shared account-summary query
 * (GET /api/execution/account-summary). Amounts are token quantities, so
 * they render via `trimDecimal` (not `$`-formatted).
 *
 * Paper-mode caveat: these are the REAL on-chain wallet balances — paper
 * orders are simulated in our DB and never move them. We label that so a
 * paper user isn't confused why a paper trade didn't change the balance.
 */

import { useAccountSummary } from '../../hooks/useExecution';
import { trimDecimal } from '../../lib/format';
import { AssetBadge, Card } from '../ui';

export function BalanceCard({ paper }: { paper: boolean }) {
  const summary = useAccountSummary();
  const balances = (summary.data?.spot_balances ?? []).filter((b) => Number(b.total) > 0);

  return (
    <Card pad={false}>
      <div className="px-[18px] py-3.5 border-b border-line-2 flex justify-between items-center">
        <span className="text-[15px] font-semibold">Balance</span>
        <span className="font-mono text-[10px] text-t4">spot wallet</span>
      </div>
      <div className="p-[18px]">
        {summary.isLoading && <p className="text-t3 text-sm">Loading balance…</p>}
        {summary.isError && (
          <p className="text-t3 text-sm">
            Balance unavailable — the SoDEX gateway didn&apos;t respond. Retry shortly.
          </p>
        )}
        {summary.data && balances.length === 0 && (
          <p className="text-t3 text-sm">No spot balance on this wallet yet.</p>
        )}
        {balances.length > 0 && (
          <div className="space-y-2.5">
            {balances.map((b) => {
              const locked = Number(b.locked) > 0;
              return (
                <div key={b.asset} className="flex items-center justify-between gap-3">
                  <AssetBadge asset={b.asset} size="sm" />
                  <div className="text-right">
                    <div className="font-mono tabular-nums text-[13px]">
                      {trimDecimal(b.available)}
                      <span className="text-t4 text-[10px]"> avail</span>
                    </div>
                    {locked && (
                      <div className="font-mono tabular-nums text-[10px] text-t4">
                        {trimDecimal(b.total)} total · {trimDecimal(b.locked)} locked
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {paper && (
          <p className="font-mono text-[10px] text-warn leading-[1.5] mt-3 pt-3 border-t border-line-1">
            Paper mode — this is your real wallet. Paper orders are simulated and don&apos;t move it.
          </p>
        )}
      </div>
    </Card>
  );
}
