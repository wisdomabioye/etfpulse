import { useState } from 'react';

import { Link } from 'react-router-dom';

import { useAdminMetrics } from '../api/queries';
import { loadAdminKey, saveAdminKey } from '../auth/adminKey';
import { AdminKeyForm } from '../components/admin/AdminKeyForm';
import { EmptyState, Kicker, PageHeader } from '../components/ui';
import { formatAgo } from '../lib/format';

import { MetricsBody, MetricsError, MetricsSkeleton } from './admin/metrics';
import { ActionsPanel, DeliveryTracePanel } from './admin/sections';

/**
 * /admin — operator dashboard for reaper + scheduler visibility plus
 * the full admin-mutation surface (signal trigger, retry-ai, eval,
 * rotate secret, user paper-trade flip, wallet unbind, execution
 * halt/resume, sodex symbols refresh, delivery trace).
 *
 * Unlisted from TopNav by design. The X-Admin-Key gate is the same one
 * `/api/admin/*` enforces server-side; storing the key in sessionStorage
 * keeps it within the tab without leaking to disk. A clear-key button
 * lets the operator drop the credential when they're done.
 *
 * This file is intentionally THIN — just the page shell + key gate.
 * Action sections live in `./admin/sections.tsx`, result displays in
 * `./admin/results.tsx`, metrics dashboard in `./admin/metrics.tsx`.
 * The split (#187) keeps each module focused on one concern (sections =
 * mutations, results = success-state UI, metrics = read-only dashboard)
 * so a future edit touches one file, not the 1000-line monster this
 * page used to be.
 *
 * The page is lazy-loaded by App.tsx (#186) so the operator UI doesn't
 * bloat the public-page bundle for the 99%+ of visitors who never hit
 * `/admin`.
 */

export function Admin() {
  const [keyInput, setKeyInput] = useState(loadAdminKey);
  const [activeKey, setActiveKey] = useState(loadAdminKey);

  const query = useAdminMetrics(activeKey);

  const handleSubmit = () => {
    saveAdminKey(keyInput);
    setActiveKey(keyInput);
  };

  const handleClear = () => {
    saveAdminKey('');
    setKeyInput('');
    setActiveKey('');
  };

  return (
    <div className="p-6 space-y-6">
      <PageHeader
        title="Operator Dashboard"
        eyebrow={<Kicker dot dotColor="warn">Admin · Internal</Kicker>}
        meta={
          query.isFetching && activeKey
            ? 'Refreshing…'
            : query.dataUpdatedAt > 0
              ? `Updated ${formatAgo(new Date(query.dataUpdatedAt).toISOString())}`
              : null
        }
      />

      {/* Key gate — shared component, used on every admin surface. */}
      <AdminKeyForm
        keyInput={keyInput}
        onInputChange={setKeyInput}
        activeKey={activeKey}
        onSubmit={handleSubmit}
        onClear={handleClear}
      />

      {/* Body --------------------------------------------------------------- */}
      {!activeKey && (
        <EmptyState
          title="Enter your admin key to view metrics."
          hint="Key is held in sessionStorage and dropped when the tab closes."
        />
      )}

      {activeKey && query.isLoading && <MetricsSkeleton />}

      {activeKey && query.error && <MetricsError error={query.error} />}

      {activeKey && (
        <Link
          to="/admin/backtest"
          className="block border border-border-2 bg-bg-2 rounded-md p-4 hover:border-accent transition-colors"
        >
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-text-1 font-medium text-[14px]">
                Run backtest
              </div>
              <div className="text-text-3 text-[12px] mt-1">
                Replay detectors over a historical window with override
                configs. Read-only, never writes signals.
              </div>
            </div>
            <span className="text-accent text-[13px] font-mono">→</span>
          </div>
        </Link>
      )}

      {activeKey && <ActionsPanel adminKey={activeKey} />}

      {activeKey && <DeliveryTracePanel adminKey={activeKey} />}

      {activeKey && query.data && <MetricsBody data={query.data} />}
    </div>
  );
}
