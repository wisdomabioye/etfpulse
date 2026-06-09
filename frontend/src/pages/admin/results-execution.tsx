/**
 * Admin action result-display components — execution/delivery ops (#187 split).
 *
 * Each component is the success-state UI for one specific admin mutation.
 * Co-located here because they share a visual vocabulary (Callout + tone +
 * ResultLine) but each has a unique result shape — extracting a generic
 * shell would push complexity inward without saving lines.
 */

import type {
  DeliveryTraceResult,
  HaltExecutionResult,
  ResumeExecutionResult,
  SetPaperTradeResult,
  SymbolsRefreshResult,
  UnbindWalletResult,
} from '../../api/queries';
import { Callout, EmptyState } from '../../components/ui';
import { ResultLine } from './results-shared';

export function PaperTradeResult({ result }: { result: SetPaperTradeResult }) {
  return (
    <Callout tone="pos">
      <div className="font-mono text-[12px]">
        User <span className="text-t1">#{result.user_id}</span> →{' '}
        <code className="font-mono text-t1">
          paper_trade = {String(result.paper_trade)}
        </code>
      </div>
    </Callout>
  );
}

export function UnbindResult({ result }: { result: UnbindWalletResult }) {
  const tone = result.was_bound ? 'pos' : 'info';
  return (
    <Callout tone={tone}>
      <div className="font-mono text-[12px] space-y-1">
        <div>
          User <span className="text-t1">#{result.user_id}</span> ·{' '}
          <code className="font-mono">was_bound = {String(result.was_bound)}</code>
        </div>
        {result.was_bound ? (
          <div className="text-t3 break-all">
            Cleared wallet: <span className="text-t1">{result.previous_wallet_address}</span>
          </div>
        ) : (
          <div className="text-t3">No-op — user was already unbound.</div>
        )}
      </div>
    </Callout>
  );
}

export function HaltResult({ result }: { result: HaltExecutionResult }) {
  const tone = result.already_active ? 'warn' : 'pos';
  return (
    <Callout tone={tone}>
      <div className="font-mono text-[12px] space-y-1">
        <div>
          Scope <span className="text-t1">{result.scope}</span> · breaker{' '}
          <span className="text-t1">#{result.breaker_id}</span> ·{' '}
          <code className="font-mono">
            already_active = {String(result.already_active)}
          </code>
        </div>
        {result.already_active && result.existing_triggered_at && (
          <div className="text-t3">
            Existing breaker triggered at{' '}
            <span className="text-t1">{result.existing_triggered_at}</span>
          </div>
        )}
        {result.already_active && result.existing_details && (
          <div className="text-t3 break-words">
            Existing details:{' '}
            <code className="font-mono text-t1">
              {JSON.stringify(result.existing_details)}
            </code>
          </div>
        )}
      </div>
    </Callout>
  );
}

export function ResumeResult({ result }: { result: ResumeExecutionResult }) {
  const tone = result.rowcount > 0 ? 'pos' : 'info';
  return (
    <Callout tone={tone}>
      <div className="font-mono text-[12px]">
        Scope <span className="text-t1">{result.scope}</span> · resolved{' '}
        <span className="text-t1">{result.rowcount}</span> breaker
        {result.rowcount === 1 ? '' : 's'}
        {result.rowcount === 0 && (
          <span className="text-t3"> — nothing was active.</span>
        )}
      </div>
    </Callout>
  );
}

export function SymbolsRefreshResultDisplay({ result }: { result: SymbolsRefreshResult }) {
  const total =
    result.spot_inserted + result.spot_updated + result.perps_inserted + result.perps_updated;
  const parseErrors = (result.spot_parse_errors ?? 0) + (result.perps_parse_errors ?? 0);
  const tone: 'pos' | 'warn' | 'info' =
    result.errors > 0 || parseErrors > 0 ? 'warn' : total === 0 ? 'info' : 'pos';
  return (
    <Callout tone={tone}>
      <div className="font-mono text-[12px] space-y-1">
        <div className="flex flex-wrap gap-x-6 gap-y-1">
          <ResultLine label="Spot inserted" value={result.spot_inserted} />
          <ResultLine label="Spot updated" value={result.spot_updated} />
          <ResultLine label="Perps inserted" value={result.perps_inserted} />
          <ResultLine label="Perps updated" value={result.perps_updated} />
          <ResultLine
            label="Errors"
            value={result.errors}
            tone={result.errors > 0 ? 'warn' : undefined}
          />
          {parseErrors > 0 && (
            <ResultLine label="Parse errors" value={parseErrors} tone="warn" />
          )}
        </div>
        {total === 0 && result.errors === 0 && (
          <div className="text-t3 text-[11px]">
            No new or changed symbols — cache already up to date.
          </div>
        )}
      </div>
    </Callout>
  );
}

export function DeliveryTraceResultDisplay({ result }: { result: DeliveryTraceResult }) {
  return (
    <div className="space-y-3">
      <Callout tone="info">
        <div className="font-mono text-[12px] space-y-1">
          <div>
            Signal <span className="text-t1">#{result.signal_id}</span> ·{' '}
            <span className="text-t1">{result.signal_asset}</span> ·{' '}
            <span className="text-t1">{result.signal_type}</span> · confidence{' '}
            <span className="text-t1">{result.signal_confidence ?? 'null'}</span> ·
            status <span className="text-t1">{result.signal_status}</span>
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-1">
            <ResultLine label="Matched" value={result.matched_count} />
            <ResultLine label="Delivery rows" value={result.delivery_count} />
            <ResultLine label="Delivered" value={result.delivered_count} />
            <ResultLine
              label="Pending"
              value={result.pending_count}
              tone={result.pending_count > 0 ? 'warn' : undefined}
            />
            <ResultLine
              label="Failed"
              value={result.failed_count}
              tone={result.failed_count > 0 ? 'warn' : undefined}
            />
            <ResultLine label="Skipped" value={result.skipped_count} />
          </div>
        </div>
      </Callout>
      {result.recipients.length === 0 ? (
        <EmptyState
          title="No recipients evaluated."
          hint="Fan-out hasn't run for this signal yet, or no users/groups exist."
        />
      ) : (
        <div className="overflow-x-auto border border-line-2 rounded-md">
          <table className="min-w-full font-mono text-[11px]">
            <thead className="bg-bg-3 text-t3 uppercase tracking-[0.1em] text-[10px]">
              <tr>
                <th className="px-3 py-2 text-left">Kind</th>
                <th className="px-3 py-2 text-left">Target</th>
                <th className="px-3 py-2 text-left">Matched</th>
                <th className="px-3 py-2 text-left">Exclude reason</th>
                <th className="px-3 py-2 text-left">Delivery</th>
                <th className="px-3 py-2 text-left">Attempts / Error</th>
              </tr>
            </thead>
            <tbody>
              {result.recipients.map((r) => (
                <tr
                  key={`${r.kind}-${r.target_id}`}
                  className="border-t border-line-2"
                >
                  <td className="px-3 py-2 text-t2">{r.kind}</td>
                  <td className="px-3 py-2 text-t1 break-all">
                    #{r.target_id} · {r.target_label}
                  </td>
                  <td className="px-3 py-2">
                    {r.matched ? (
                      <span className="text-win">yes</span>
                    ) : (
                      <span className="text-t3">no</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-t3 break-words">
                    {r.exclude_reason ?? '—'}
                  </td>
                  <td className="px-3 py-2 text-t2">{r.delivery_status ?? '—'}</td>
                  <td className="px-3 py-2 text-t3 break-words">
                    {r.delivery_attempts !== null ? `${r.delivery_attempts}× ` : ''}
                    {r.delivery_error ?? ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
