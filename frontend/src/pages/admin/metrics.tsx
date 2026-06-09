/**
 * Admin metrics dashboard components (#187 split).
 *
 * Read-only display of `GET /api/admin/metrics`. MetricsBody is the
 * composer; SchedulerTable + StatTile-grids handle subsections.
 */

import type { ReactNode } from 'react';

import { ApiError } from '../../api/client';
import type { AdminMetrics, SchedulerJobInfo } from '../../api/types';
import { EmptyState, Kicker, Skeleton, StatTile } from '../../components/ui';

export function MetricsSkeleton() {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {[0, 1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-[88px]" />
      ))}
    </div>
  );
}

export function MetricsError({ error }: { error: Error }) {
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

export function MetricsBody({ data }: { data: AdminMetrics }) {
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
    <div className="border border-line-2 bg-bg-2 rounded-md overflow-hidden">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="bg-bg-3 text-t3 font-mono text-[10px] uppercase tracking-[0.1em]">
            <th className="text-left px-4 py-2">Job ID</th>
            <th className="text-left px-4 py-2">Trigger</th>
            <th className="text-left px-4 py-2">Next run (UTC)</th>
            <th className="text-left px-4 py-2">State</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id} className="border-t border-line-2">
              <td className="px-4 py-2 font-mono text-t1">{job.id}</td>
              <td className="px-4 py-2 font-mono text-[11px] text-t2">{job.trigger}</td>
              <td className="px-4 py-2 font-mono tabular-nums text-t2">
                {job.next_run_at ?? '—'}
              </td>
              <td className="px-4 py-2 text-t3">
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
  const color = warn ? 'text-warn' : 'text-t3';
  return <div className={`text-[12px] font-mono ${color}`}>{children}</div>;
}
