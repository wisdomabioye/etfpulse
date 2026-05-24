import { useState } from 'react';
import type { FormEvent } from 'react';

import { useAdminMetrics } from '../api/queries';
import { Button, EmptyState, Kicker, PageHeader } from '../components/ui';
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
          query.isFetching && activeKey
            ? 'Refreshing…'
            : query.dataUpdatedAt > 0
              ? `Updated ${formatAgo(new Date(query.dataUpdatedAt).toISOString())}`
              : null
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

      {activeKey && <DeliveryTracePanel adminKey={activeKey} />}

      {activeKey && query.data && <MetricsBody data={query.data} />}
    </div>
  );
}
