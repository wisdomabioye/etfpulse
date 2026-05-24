/**
 * Admin action result-display components (#187 split).
 *
 * Each component is the success-state UI for one specific admin mutation.
 * Co-located here because they share a visual vocabulary (Callout + tone +
 * ResultLine) but each has a unique result shape — extracting a generic
 * shell would push complexity inward without saving lines.
 *
 * `ResultLine` is internal (only consumed by results in this module).
 * If a non-result call site needs it, promote to `components/ui/`.
 */

import type {
  DeliveryTraceResult,
  EvalOutcomesResult,
  HaltExecutionResult,
  ResumeExecutionResult,
  RetryAiResult,
  RotateWebhookSecretResult,
  SetPaperTradeResult,
  SymbolsRefreshResult,
  TriggerCycleResponse,
  UnbindWalletResult,
} from '../../api/queries';
import { Callout, EmptyState } from '../../components/ui';

/** One label/value row inside a result Callout. `tone='warn'` colors the
 *  value yellow + bold to draw the operator's eye when something needs
 *  attention. */
function ResultLine({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | string;
  tone?: 'warn';
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-text-3">{label}</span>
      <span className={tone === 'warn' ? 'text-warn font-semibold' : 'text-text-1'}>
        {value}
      </span>
    </div>
  );
}

export function TriggerResult({ result }: { result: TriggerCycleResponse }) {
  const r = result;
  return (
    <Callout tone="pos">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-1 font-mono text-[12px]">
        <ResultLine label="Signals new" value={r.signals_new} />
        <ResultLine label="Duplicate" value={r.signals_duplicate} />
        <ResultLine
          label="AI succeeded"
          value={r.ai_succeeded}
          tone={r.ai_succeeded === 0 && r.signals_new > 0 ? 'warn' : undefined}
        />
        <ResultLine
          label="AI failed"
          value={r.ai_failed}
          tone={r.ai_failed > 0 ? 'warn' : undefined}
        />
        <ResultLine
          label="BTC price"
          value={r.prices.BTC ? `$${r.prices.BTC.price}` : '—'}
        />
        <ResultLine
          label="ETH price"
          value={r.prices.ETH ? `$${r.prices.ETH.price}` : '—'}
        />
        <ResultLine label="Detectors run" value={r.detectors_run} />
        <ResultLine
          label="Regime"
          value={r.regime ? `${r.regime.regime} · ${r.regime.signal_posture}` : '—'}
        />
      </div>
      {r.ai_failed > 0 && r.signals_new > 0 && (
        <div className="mt-2 text-[11px] text-text-3">
          AI enrichment failed for {r.ai_failed} signal(s) — common causes: insufficient
          OpenRouter credits (HTTP 402), schema-mismatch on response, or daily call cap
          reached. Check server logs.
        </div>
      )}
    </Callout>
  );
}

export function RetryAiResultDisplay({ result }: { result: RetryAiResult }) {
  // Tone selection mirrors the operator-facing meaning:
  //   - all updated → green (full success)
  //   - some updated, some failed → warn (partial success, more clicks help)
  //   - 0 scanned → info (backlog empty — nothing to do)
  //   - all failed → neg (every retry hit the same wall — fix root cause first)
  const tone: 'pos' | 'warn' | 'info' | 'neg' =
    result.scanned === 0
      ? 'info'
      : result.updated === result.scanned
        ? 'pos'
        : result.updated > 0
          ? 'warn'
          : 'neg';
  return (
    <Callout tone={tone}>
      <div className="font-mono text-[12px] space-y-2">
        <div className="flex flex-wrap gap-x-6 gap-y-1">
          <ResultLine label="Scanned" value={result.scanned} />
          <ResultLine label="Updated" value={result.updated} />
          <ResultLine
            label="Failed"
            value={result.failed}
            tone={result.failed > 0 ? 'warn' : undefined}
          />
        </div>
        {result.scanned === 0 && (
          <div className="text-[11px] text-text-3">
            No NULL-AI signals to retry — backlog is empty.
          </div>
        )}
        {result.error_samples.length > 0 && (
          <div className="space-y-1 pt-1 border-t border-border-2">
            <div className="text-text-3 uppercase tracking-[0.1em] text-[10px]">
              Failure samples (first {result.error_samples.length})
            </div>
            {result.error_samples.map((s) => (
              <div key={s.signal_id} className="break-words text-text-2">
                <span className="text-text-3">#{s.signal_id}</span>{' '}
                <span className="text-warn">{s.kind}</span>{' '}
                <span className="text-text-3">— {s.detail}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Callout>
  );
}

export function EvalOutcomesResultDisplay({ result }: { result: EvalOutcomesResult }) {
  // Tone selection mirrors operator-facing meaning, same convention as
  // RetryAiResultDisplay:
  //   - 0 candidates → info (backlog empty — nothing to do)
  //   - all evaluated → green (full success)
  //   - partial / errored / skipped → warn (re-fire or investigate)
  const tone: 'pos' | 'warn' | 'info' =
    result.candidates === 0
      ? 'info'
      : result.evaluated === result.candidates
        ? 'pos'
        : 'warn';
  const skipped =
    result.skipped_no_direction +
    result.skipped_unknown_asset +
    result.skipped_no_klines +
    result.skipped_no_bars_in_window;
  return (
    <Callout tone={tone}>
      <div className="font-mono text-[12px] space-y-2">
        <div className="flex flex-wrap gap-x-6 gap-y-1">
          <ResultLine label="Candidates" value={result.candidates} />
          <ResultLine label="Evaluated" value={result.evaluated} />
          <ResultLine
            label="Skipped"
            value={skipped}
            tone={skipped > 0 ? 'warn' : undefined}
          />
          <ResultLine
            label="Errored"
            value={result.errored}
            tone={result.errored > 0 ? 'warn' : undefined}
          />
          <ResultLine
            label="Remaining"
            value={result.remaining}
            tone={result.remaining > 0 ? 'warn' : undefined}
          />
        </div>
        {result.candidates === 0 && (
          <div className="text-[11px] text-text-3">
            No aged signals to score — backlog is empty (or the next eligible
            signal is still under 72h old).
          </div>
        )}
        {result.remaining > 0 && (
          <div className="text-[11px] text-text-2">
            {result.remaining} more eligible signal{result.remaining === 1 ? '' : 's'} left
            after this batch — click again to continue draining.
          </div>
        )}
        {skipped > 0 && (
          <div className="text-[11px] text-text-3 pt-1 border-t border-border-2">
            Skipped breakdown: no_direction={result.skipped_no_direction} ·
            unknown_asset={result.skipped_unknown_asset} ·
            no_klines={result.skipped_no_klines} ·
            no_bars_in_window={result.skipped_no_bars_in_window}
          </div>
        )}
      </div>
    </Callout>
  );
}

export function RotateResult({ result }: { result: RotateWebhookSecretResult }) {
  return (
    <Callout tone="info">
      <div className="font-mono text-[12px] space-y-2">
        <div className="text-text-3 uppercase tracking-[0.1em] text-[10px]">
          New webhook secret (one-time display)
        </div>
        <div className="break-all text-text-1 bg-bg-3 border border-border-2 rounded px-3 py-2">
          {result.secret}
        </div>
        <div className="text-text-3">{result.note}</div>
      </div>
    </Callout>
  );
}

export function PaperTradeResult({ result }: { result: SetPaperTradeResult }) {
  return (
    <Callout tone="pos">
      <div className="font-mono text-[12px]">
        User <span className="text-text-1">#{result.user_id}</span> →{' '}
        <code className="font-mono text-text-1">
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
          User <span className="text-text-1">#{result.user_id}</span> ·{' '}
          <code className="font-mono">was_bound = {String(result.was_bound)}</code>
        </div>
        {result.was_bound ? (
          <div className="text-text-3 break-all">
            Cleared wallet: <span className="text-text-1">{result.previous_wallet_address}</span>
          </div>
        ) : (
          <div className="text-text-3">No-op — user was already unbound.</div>
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
          Scope <span className="text-text-1">{result.scope}</span> · breaker{' '}
          <span className="text-text-1">#{result.breaker_id}</span> ·{' '}
          <code className="font-mono">
            already_active = {String(result.already_active)}
          </code>
        </div>
        {result.already_active && result.existing_triggered_at && (
          <div className="text-text-3">
            Existing breaker triggered at{' '}
            <span className="text-text-1">{result.existing_triggered_at}</span>
          </div>
        )}
        {result.already_active && result.existing_details && (
          <div className="text-text-3 break-words">
            Existing details:{' '}
            <code className="font-mono text-text-1">
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
        Scope <span className="text-text-1">{result.scope}</span> · resolved{' '}
        <span className="text-text-1">{result.rowcount}</span> breaker
        {result.rowcount === 1 ? '' : 's'}
        {result.rowcount === 0 && (
          <span className="text-text-3"> — nothing was active.</span>
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
          <div className="text-text-3 text-[11px]">
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
            Signal <span className="text-text-1">#{result.signal_id}</span> ·{' '}
            <span className="text-text-1">{result.signal_asset}</span> ·{' '}
            <span className="text-text-1">{result.signal_type}</span> · confidence{' '}
            <span className="text-text-1">{result.signal_confidence ?? 'null'}</span> ·
            status <span className="text-text-1">{result.signal_status}</span>
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
        <div className="overflow-x-auto border border-border-2 rounded-md">
          <table className="min-w-full font-mono text-[11px]">
            <thead className="bg-bg-3 text-text-3 uppercase tracking-[0.1em] text-[10px]">
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
                  className="border-t border-border-2"
                >
                  <td className="px-3 py-2 text-text-2">{r.kind}</td>
                  <td className="px-3 py-2 text-text-1 break-all">
                    #{r.target_id} · {r.target_label}
                  </td>
                  <td className="px-3 py-2">
                    {r.matched ? (
                      <span className="text-pos">yes</span>
                    ) : (
                      <span className="text-text-3">no</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-text-3 break-words">
                    {r.exclude_reason ?? '—'}
                  </td>
                  <td className="px-3 py-2 text-text-2">{r.delivery_status ?? '—'}</td>
                  <td className="px-3 py-2 text-text-3 break-words">
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
