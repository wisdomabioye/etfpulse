/**
 * Admin action result-display components — signal-pipeline ops (#187 split).
 *
 * Each component is the success-state UI for one specific admin mutation.
 * Co-located here because they share a visual vocabulary (Callout + tone +
 * ResultLine) but each has a unique result shape — extracting a generic
 * shell would push complexity inward without saving lines.
 */

import type {
  EvalOutcomesResult,
  RetryAiResult,
  RotateWebhookSecretResult,
  TriggerCycleResponse,
} from '../../api/queries';
import { Callout } from '../../components/ui';
import { ResultLine } from './results-shared';

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
        <div className="mt-2 text-[11px] text-t3">
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
          <div className="text-[11px] text-t3">
            No NULL-AI signals to retry — backlog is empty.
          </div>
        )}
        {result.error_samples.length > 0 && (
          <div className="space-y-1 pt-1 border-t border-line-2">
            <div className="text-t3 uppercase tracking-[0.1em] text-[10px]">
              Failure samples (first {result.error_samples.length})
            </div>
            {result.error_samples.map((s) => (
              <div key={s.signal_id} className="break-words text-t2">
                <span className="text-t3">#{s.signal_id}</span>{' '}
                <span className="text-warn">{s.kind}</span>{' '}
                <span className="text-t3">— {s.detail}</span>
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
          <div className="text-[11px] text-t3">
            No aged signals to score — backlog is empty (or the next eligible
            signal is still under 72h old).
          </div>
        )}
        {result.remaining > 0 && (
          <div className="text-[11px] text-t2">
            {result.remaining} more eligible signal{result.remaining === 1 ? '' : 's'} left
            after this batch — click again to continue draining.
          </div>
        )}
        {skipped > 0 && (
          <div className="text-[11px] text-t3 pt-1 border-t border-line-2">
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
        <div className="text-t3 uppercase tracking-[0.1em] text-[10px]">
          New webhook secret (one-time display)
        </div>
        <div className="break-all text-t1 bg-bg-3 border border-line-2 rounded px-3 py-2">
          {result.secret}
        </div>
        <div className="text-t3">{result.note}</div>
      </div>
    </Callout>
  );
}
