import { useState } from 'react';
import { useAdminMetrics } from '../api/queries';
import { ApiError } from '../api/client';
import type { AdminMetrics, SchedulerJobInfo } from '../api/types';
import {
  Button,
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

  const handleSubmit = (e: React.FormEvent) => {
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

      {activeKey && query.data && <MetricsBody data={query.data} />}
    </div>
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

function MetricsError({ error }: { error: unknown }) {
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
  return (
    <EmptyState
      title="Could not load metrics."
      hint={error instanceof Error ? error.message : 'Unknown error'}
    />
  );
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

function Hint({ warn, children }: { warn?: boolean; children: React.ReactNode }) {
  const color = warn ? 'text-warn' : 'text-text-3';
  return <div className={`text-[12px] font-mono ${color}`}>{children}</div>;
}
