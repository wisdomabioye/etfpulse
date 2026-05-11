import { useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import {
  useAdminMetrics,
  useEvalOutcomes,
  useRetryAiNullSignals,
  useRotateWebhookSecret,
  useTriggerSignalCycle,
  type EvalOutcomesResult,
  type RetryAiResult,
  type RotateWebhookSecretResult,
  type TriggerCycleResponse,
} from '../api/queries';
import { ApiError } from '../api/client';
import type { AdminMetrics, SchedulerJobInfo } from '../api/types';
import {
  Button,
  Callout,
  EmptyState,
  Kicker,
  PageHeader,
  Skeleton,
  StatTile,
} from '../components/ui';
import { formatAgo } from '../lib/format';

/**
 * /admin — operator dashboard for reaper + scheduler visibility.
 *
 * Unlisted from TopNav by design. The X-Admin-Key gate is the same one
 * `/api/admin/*` enforces server-side; storing the key in sessionStorage
 * keeps it within the tab without leaking to disk. A clear-key button
 * lets the operator drop the credential when they're done.
 *
 * The page does NOT mutate anything — it's a read surface on the same
 * GET /api/admin/metrics endpoint. Auto-refreshes every 15s.
 */

const SESSION_KEY = 'etfpulse:admin_key';

function loadKey(): string {
  try {
    return sessionStorage.getItem(SESSION_KEY) ?? '';
  } catch {
    return '';
  }
}

function persistKey(key: string) {
  try {
    if (key) sessionStorage.setItem(SESSION_KEY, key);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // ignore — private mode / disabled storage. Key still works for this render.
  }
}

export function Admin() {
  const [keyInput, setKeyInput] = useState(loadKey);
  const [activeKey, setActiveKey] = useState(loadKey);

  const query = useAdminMetrics(activeKey);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    persistKey(keyInput);
    setActiveKey(keyInput);
  };

  const handleClear = () => {
    persistKey('');
    setKeyInput('');
    setActiveKey('');
  };

  return (
    <div className="p-6 space-y-6">
      <PageHeader
        title="Operator Dashboard"
        eyebrow={<Kicker dot dotColor="warn">Admin · Internal</Kicker>}
        meta={
          query.isFetching && activeKey ? 'Refreshing…' : query.dataUpdatedAt > 0 ? `Updated ${formatAgo(new Date(query.dataUpdatedAt).toISOString())}` : null
        }
      />

      {/* Key gate ----------------------------------------------------------- */}
      <form
        onSubmit={handleSubmit}
        className="flex flex-wrap items-end gap-3 border border-border-2 bg-bg-2 rounded-md p-4"
      >
        <label className="flex-1 min-w-[240px]">
          <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mb-2">
            Admin Key
          </div>
          <input
            type="password"
            autoComplete="off"
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            placeholder="X-Admin-Key header value"
            className="w-full bg-bg-3 text-text-1 border border-border-3 rounded-[5px] px-3 py-2 text-[13px] font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          />
        </label>
        <Button type="submit" variant="primary">
          {activeKey ? 'Reload' : 'Unlock'}
        </Button>
        {activeKey && (
          <Button type="button" variant="ghost" onClick={handleClear}>
            Clear key
          </Button>
        )}
      </form>

      {/* Body --------------------------------------------------------------- */}
      {!activeKey && (
        <EmptyState
          title="Enter your admin key to view metrics."
          hint="Key is held in sessionStorage and dropped when the tab closes."
        />
      )}

      {activeKey && query.isLoading && <MetricsSkeleton />}

      {activeKey && query.error && <MetricsError error={query.error} />}

      {activeKey && <ActionsPanel adminKey={activeKey} />}

      {activeKey && query.data && <MetricsBody data={query.data} />}
    </div>
  );
}

/** Operator-actionable buttons. The metrics dashboard is read-only;
 *  these are the mutations: kick the daily cycle, rotate the Telegram
 *  webhook secret. Both are admin-gated server-side; the same key from
 *  the gate above is reused. Failures stay inline (Callout) rather than
 *  redirecting — operators want to see exactly what the API returned. */
function ActionsPanel({ adminKey }: { adminKey: string }) {
  const trigger = useTriggerSignalCycle(adminKey);
  const retryAi = useRetryAiNullSignals(adminKey);
  const evalOutcomes = useEvalOutcomes(adminKey);
  const rotate = useRotateWebhookSecret(adminKey);
  const [confirmRotate, setConfirmRotate] = useState(false);

  return (
    <section className="border border-border-2 bg-bg-2 rounded-md p-4 space-y-4">
      <Kicker>Actions</Kicker>

      {/* Trigger cycle ----------------------------------------------------- */}
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex-1 min-w-[260px]">
          <div className="text-[14px] font-semibold text-text-1">Trigger signal cycle</div>
          <div className="text-[12px] text-text-3">
            Runs the same code path as the scheduled cron — ingest flows + news, run all 5 detectors, enrich with AI. Synchronous; may take ~10–60s.
          </div>
        </div>
        <Button
          type="button"
          variant="primary"
          onClick={() => trigger.mutate()}
          disabled={trigger.isPending}
        >
          {trigger.isPending ? 'Running…' : 'Run cycle'}
        </Button>
      </div>
      {trigger.error && <ActionError error={trigger.error} />}
      {trigger.data && <TriggerResult result={trigger.data} />}

      {/* Retry AI on stale NULL-AI signals -------------------------------- */}
      <div className="flex flex-wrap items-start gap-3 border-t border-border-2 pt-4">
        <div className="flex-1 min-w-[260px]">
          <div className="text-[14px] font-semibold text-text-1">
            Retry AI on stale signals
          </div>
          <div className="text-[12px] text-text-3">
            Re-runs OpenRouter on Signals with NULL <code className="font-mono">ai_analysis</code> (stranded by an earlier credit-out / quota / schema failure — the daily cycle never retries existing rows). Caps at 10 calls per click.
          </div>
        </div>
        <Button
          type="button"
          variant="primary"
          onClick={() => retryAi.mutate(10)}
          disabled={retryAi.isPending}
        >
          {retryAi.isPending ? 'Retrying…' : 'Retry AI (10)'}
        </Button>
      </div>
      {retryAi.error && <ActionError error={retryAi.error} />}
      {retryAi.data && <RetryAiResultDisplay result={retryAi.data} />}

      {/* Evaluate outcomes on demand -------------------------------------- */}
      <div className="flex flex-wrap items-start gap-3 border-t border-border-2 pt-4">
        <div className="flex-1 min-w-[260px]">
          <div className="text-[14px] font-semibold text-text-1">
            Evaluate outcomes now
          </div>
          <div className="text-[12px] text-text-3">
            Scores aged signals (≥72h old, with price + confidence) into <code className="font-mono">SignalOutcome</code> rows. The scheduled job runs hourly capped at <code className="font-mono">OUTCOME_EVAL_BATCH_LIMIT</code> (default 50); this button uses a smaller per-click cap of 20 to stay safely below SoSoValue's rate limit. Re-fire while <code className="font-mono">remaining &gt; 0</code> to drain a backlog.
          </div>
        </div>
        <Button
          type="button"
          variant="primary"
          onClick={() => evalOutcomes.mutate(20)}
          disabled={evalOutcomes.isPending}
        >
          {evalOutcomes.isPending ? 'Evaluating…' : 'Evaluate (20)'}
        </Button>
      </div>
      {evalOutcomes.error && <ActionError error={evalOutcomes.error} />}
      {evalOutcomes.data && <EvalOutcomesResultDisplay result={evalOutcomes.data} />}

      {/* Rotate webhook secret -------------------------------------------- */}
      <div className="flex flex-wrap items-start gap-3 border-t border-border-2 pt-4">
        <div className="flex-1 min-w-[260px]">
          <div className="text-[14px] font-semibold text-text-1">Rotate Telegram webhook secret</div>
          <div className="text-[12px] text-text-3">
            Issues a fresh secret and pushes it to Telegram race-free. Mirror the new value into <code className="font-mono">TELEGRAM_WEBHOOK_SECRET</code> before the next container restart, or the boot-time value will overwrite it.
          </div>
        </div>
        {!confirmRotate ? (
          <Button
            type="button"
            variant="secondary"
            onClick={() => setConfirmRotate(true)}
            disabled={rotate.isPending}
          >
            Rotate…
          </Button>
        ) : (
          <div className="flex gap-2">
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                rotate.mutate();
                setConfirmRotate(false);
              }}
              disabled={rotate.isPending}
            >
              {rotate.isPending ? 'Rotating…' : 'Confirm rotate'}
            </Button>
            <Button type="button" variant="ghost" onClick={() => setConfirmRotate(false)}>
              Cancel
            </Button>
          </div>
        )}
      </div>
      {rotate.error && <ActionError error={rotate.error} />}
      {rotate.data && <RotateResult result={rotate.data} />}
    </section>
  );
}

function ActionError({ error }: { error: Error }) {
  const detail =
    error instanceof ApiError ? `HTTP ${error.status} · ${error.detail}` : error.message;
  return (
    <Callout tone="neg">
      <span className="font-mono text-[12px]">{detail}</span>
    </Callout>
  );
}

function TriggerResult({ result }: { result: TriggerCycleResponse }) {
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

function RetryAiResultDisplay({ result }: { result: RetryAiResult }) {
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

function EvalOutcomesResultDisplay({ result }: { result: EvalOutcomesResult }) {
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

function RotateResult({ result }: { result: RotateWebhookSecretResult }) {
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

function MetricsSkeleton() {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {[0, 1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-[88px]" />
      ))}
    </div>
  );
}

function MetricsError({ error }: { error: Error }) {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return (
        <EmptyState
          title="Invalid admin key."
          hint="Server rejected the X-Admin-Key header. Double-check the value or rotate it server-side."
        />
      );
    }
    if (error.status === 503) {
      return (
        <EmptyState
          title="Admin surface disabled."
          hint="ADMIN_API_KEY is unset in the backend environment. Set it and redeploy to use this page."
        />
      );
    }
  }
  return <EmptyState title="Could not load metrics." hint={error.message} />;
}

function MetricsBody({ data }: { data: AdminMetrics }) {
  const sig = data.signal_status_counts;
  const deliv = data.delivery_status_counts;
  return (
    <>
      {/* Signal queue */}
      <section className="space-y-3">
        <Kicker>Signals</Kicker>
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
          <StatTile label="Pending" value={sig.pending} />
          <StatTile label="Alerted" value={sig.alerted} />
          <StatTile label="Expired" value={sig.expired} />
          <StatTile
            label="Overdue (unreaped)"
            value={data.signals_overdue_unreaped}
          />
          <StatTile
            label="Null AI confidence"
            value={data.signals_null_confidence}
          />
        </div>
        {data.signals_overdue_unreaped > 0 && (
          <Hint warn>
            {data.signals_overdue_unreaped} signal(s) past expires_at. Reaper ticks every 15 min — non-zero between ticks is expected, persistent non-zero means the scheduler is not advancing.
          </Hint>
        )}
      </section>

      {/* Deliveries */}
      <section className="space-y-3">
        <Kicker>Deliveries</Kicker>
        <div className="grid grid-cols-2 lg:grid-cols-6 gap-3">
          <StatTile label="Pending" value={deliv.pending} />
          <StatTile label="Delivered" value={deliv.delivered} />
          <StatTile label="Failed" value={deliv.failed} />
          <StatTile label="Skipped" value={deliv.skipped} />
          <StatTile label="Stuck pending" value={data.deliveries_stuck_pending} />
          <StatTile
            label="Reaper failures (all-time)"
            value={data.deliveries_reaper_failures}
          />
        </div>
        {data.deliveries_stuck_pending > 0 && (
          <Hint warn>
            {data.deliveries_stuck_pending} delivery row(s) exceed the stuck-pending threshold. The delivery reaper will flip these to FAILED on its next tick.
          </Hint>
        )}
      </section>

      {/* Scheduler */}
      <section className="space-y-3">
        <Kicker>Scheduler</Kicker>
        <SchedulerTable jobs={data.scheduler_jobs} />
      </section>

      {/* AI prompt versioning (#32) */}
      <section className="space-y-3">
        <Kicker>AI prompt version</Kicker>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <StatTile
            label="Active version"
            value={<span className="font-mono">{data.current_ai_prompt_version}</span>}
          />
          {Object.entries(data.signal_counts_by_prompt_version).map(([version, count]) => (
            <StatTile
              key={version}
              label={`Signals · ${version}`}
              value={count}
            />
          ))}
        </div>
        <Hint>
          Track-record can be sliced by version via{' '}
          <code className="font-mono">?ai_prompt_version={data.current_ai_prompt_version}</code>{' '}
          to avoid mixing cohorts after a prompt bump.
        </Hint>
      </section>

      {/* Webhook secrets (#40) */}
      {data.accepted_webhook_secrets !== null && (
        <section className="space-y-3">
          <Kicker>Webhook secret rotation</Kicker>
          <div className="grid grid-cols-2 gap-3">
            <StatTile label="Accepted secrets" value={data.accepted_webhook_secrets} />
          </div>
          {data.accepted_webhook_secrets > 1 && (
            <Hint warn>
              {data.accepted_webhook_secrets} secrets accepted — a rotation
              is mid-flight or didn't complete. Re-run rotation to converge
              to a single active secret.
            </Hint>
          )}
        </section>
      )}
    </>
  );
}

function SchedulerTable({ jobs }: { jobs: SchedulerJobInfo[] | null }) {
  if (jobs === null) {
    return (
      <EmptyState
        title="Scheduler disabled."
        hint="run_scheduler=false in this process. Jobs are not running here — they may be running in a separate worker process."
      />
    );
  }
  if (jobs.length === 0) {
    return (
      <EmptyState
        title="Scheduler is running but has no jobs registered."
        hint="Unexpected. Check startup logs for scheduler_disabled events."
      />
    );
  }
  return (
    <div className="border border-border-2 bg-bg-2 rounded-md overflow-hidden">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="bg-bg-3 text-text-3 font-mono text-[10px] uppercase tracking-[0.1em]">
            <th className="text-left px-4 py-2">Job ID</th>
            <th className="text-left px-4 py-2">Trigger</th>
            <th className="text-left px-4 py-2">Next run (UTC)</th>
            <th className="text-left px-4 py-2">State</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id} className="border-t border-border-2">
              <td className="px-4 py-2 font-mono text-text-1">{job.id}</td>
              <td className="px-4 py-2 font-mono text-[11px] text-text-2">{job.trigger}</td>
              <td className="px-4 py-2 font-mono tabular-nums text-text-2">
                {job.next_run_at ?? '—'}
              </td>
              <td className="px-4 py-2 text-text-3">
                {job.pending ? 'pending first dispatch' : 'scheduled'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Hint({ warn, children }: { warn?: boolean; children: ReactNode }) {
  const color = warn ? 'text-warn' : 'text-text-3';
  return <div className={`text-[12px] font-mono ${color}`}>{children}</div>;
}
