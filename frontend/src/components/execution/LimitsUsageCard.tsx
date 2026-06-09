/**
 * Risk caps + the user's usage against them (P0). Surfaces the limits that
 * otherwise only appear via a 403 risk-DENY — so the user sees headroom up
 * front. Reads GET /api/execution/limits; `asset` (the order form's selected
 * asset) adds the per-symbol bar.
 */

import { useExecutionLimits } from '../../hooks/useExecution';
import { formatPrice } from '../../lib/format';
import { Card } from '../ui';

/** One labelled usage bar. `used`/`cap` are numbers; `format` renders them. */
function UsageBar({
  label,
  used,
  cap,
  format,
}: {
  label: string;
  used: number;
  cap: number;
  format: (n: number) => string;
}) {
  // Zero-headroom = blocked, for every cap: the open-order gate denies at
  // `count >= max`, and a notional order is denied once `used >= cap` (any
  // new notional > 0 would exceed). So the "blocked" tone fires at >=, not
  // just strictly over.
  const over = cap > 0 && used >= cap;
  const pct = cap > 0 ? Math.min(100, Math.max(0, (used / cap) * 100)) : 0;
  return (
    <div>
      <div className="flex justify-between items-baseline mb-1">
        <span className="font-mono text-[10px] text-t3 tracking-[0.06em] uppercase">{label}</span>
        <span className={`font-mono tabular-nums text-[11px] ${over ? 'text-loss' : 'text-t2'}`}>
          {format(used)} <span className="text-t4">/ {format(cap)}</span>
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-bg-3 overflow-hidden">
        <div
          className={`h-full rounded-full ${over ? 'bg-loss' : 'bg-acc'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export function LimitsUsageCard({ asset }: { asset?: string }) {
  const limits = useExecutionLimits(asset);

  return (
    <Card pad={false}>
      <div className="px-[18px] py-3.5 border-b border-line-2 flex justify-between items-center">
        <span className="text-[15px] font-semibold">Limits &amp; usage</span>
        {limits.data && (
          <span className="font-mono text-[10px] text-t4">max {limits.data.max_leverage}× lev</span>
        )}
      </div>
      <div className="p-[18px] space-y-3.5">
        {limits.isLoading && <p className="text-t3 text-sm">Loading limits…</p>}
        {limits.isError && (
          <p className="text-t3 text-sm">Couldn&apos;t load limits — they still apply at submit.</p>
        )}
        {limits.data && (
          <>
            <UsageBar
              label="Open orders"
              used={limits.data.open_orders_used}
              cap={limits.data.max_open_orders}
              format={(n) => String(n)}
            />
            <UsageBar
              label="24h notional"
              used={Number(limits.data.daily_notional_used)}
              cap={Number(limits.data.daily_notional_cap)}
              format={formatPrice}
            />
            {limits.data.per_symbol_used !== null && limits.data.asset !== null && (
              <UsageBar
                label={`${limits.data.asset} · 24h notional`}
                used={Number(limits.data.per_symbol_used)}
                cap={Number(limits.data.per_symbol_cap)}
                format={formatPrice}
              />
            )}
            <p className="font-mono text-[10px] text-t4 leading-[1.5] pt-0.5">
              Notional is gross (buys + sells), leverage-excluded, over a rolling 24h window.
            </p>
          </>
        )}
      </div>
    </Card>
  );
}
