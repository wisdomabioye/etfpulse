import { useEffect, useState } from 'react';

import { useBacktest, useBacktestDetectors } from '../../api/backtest';
import { loadAdminKey, saveAdminKey } from '../../auth/adminKey';
import { AdminKeyForm } from '../../components/admin/AdminKeyForm';
import { BacktestForm, BacktestResultsCard } from '../../components/backtest';
import { Callout, EmptyState, Kicker, PageHeader } from '../../components/ui';

/**
 * /admin/backtest — operator surface for the backtest harness. Runs
 * a synchronous sweep against `POST /api/admin/backtest` and renders
 * per-detector results in a comparison table.
 *
 * Shares the `AdminKeyForm` + sessionStorage key plumbing with
 * /admin so an operator who has already unlocked one page doesn't
 * need to re-enter the key.
 */
export function BacktestPage() {
  const [keyInput, setKeyInput] = useState(loadAdminKey);
  const [activeKey, setActiveKey] = useState(loadAdminKey);

  const detectorsQuery = useBacktestDetectors(activeKey);
  const backtest = useBacktest(activeKey);

  const handleSubmit = () => {
    saveAdminKey(keyInput);
    setActiveKey(keyInput);
  };

  const handleClear = () => {
    saveAdminKey('');
    setKeyInput('');
    setActiveKey('');
    backtest.reset();
  };

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <PageHeader
        title="Backtest harness"
        eyebrow={<Kicker dot dotColor="warn">Admin · Internal</Kicker>}
        meta={backtest.isPending ? <RunningTimer /> : null}
      />

      <AdminKeyForm
        keyInput={keyInput}
        onInputChange={setKeyInput}
        activeKey={activeKey}
        onSubmit={handleSubmit}
        onClear={handleClear}
      />

      {!activeKey && (
        <EmptyState
          title="Enter your admin key to run a backtest."
          hint="Same key as /admin. Held in sessionStorage and dropped when the tab closes."
        />
      )}

      {activeKey && detectorsQuery.isError && (
        <Callout tone="warn">
          Could not load the detector registry. Confirm the admin key is
          valid and that `/api/admin/backtest/detectors` returns 200.
        </Callout>
      )}

      {activeKey && (
        <BacktestForm
          detectors={detectorsQuery.data?.detectors}
          detectorsLoading={detectorsQuery.isPending}
          busy={backtest.isPending}
          onSubmit={(req) => backtest.mutate(req)}
        />
      )}

      {backtest.isError && (
        <Callout tone="neg">
          {backtest.error instanceof Error
            ? backtest.error.message
            : 'Backtest failed — see the server logs.'}
        </Callout>
      )}

      {/* Hide the old results card while a new sweep is in flight —
          TanStack's `useMutation` keeps `data` populated from the
          previous successful run, so without this gate the operator
          would see stale numbers next to "Running…". */}
      {!backtest.isPending && backtest.data && (
        <BacktestResultsCard report={backtest.data} />
      )}
    </div>
  );
}

/**
 * Live elapsed-seconds display while the mutation is in flight. A
 * backtest sweep typically takes 10–60s; a static "Running…" label
 * leaves the operator unsure whether the request is stuck. Tick is
 * driven by setInterval, cleaned up on unmount.
 */
function RunningTimer() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <span className="font-mono text-[11px] text-t3">
      Running · {seconds}s elapsed
    </span>
  );
}
