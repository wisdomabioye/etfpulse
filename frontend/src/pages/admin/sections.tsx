/**
 * Admin action sections (#187 split).
 *
 * Each section owns one mutation hook + minimal local form state + uses
 * the `ActionSection` / `ConfirmButton` / `IdInput` / `ScopeRadios`
 * primitives from `components/admin/`. Result rendering is delegated to
 * sibling `results.tsx`.
 *
 * `ActionsPanel` composes the 9 mutation sections in a single `<section>`.
 * `DeliveryTracePanel` is a separate `<section>` (different visual
 * grouping — read-only debug surface, not an action).
 *
 * Public exports: `ActionsPanel`, `DeliveryTracePanel`, plus the 5
 * sections covered by component tests in `Admin.test.tsx`
 * (`PaperTradeSection`, `UnbindWalletSection`, `HaltExecutionSection`,
 * `ResumeExecutionSection`, `SymbolsRefreshSection`). The 4 sections
 * that wrap existing simple-mutation hooks (`TriggerCycleSection`,
 * `RetryAiSection`, `EvalOutcomesSection`, `RotateWebhookSecretSection`)
 * stay internal — they predate #186, are exercised through `ActionsPanel`
 * rendering, and have no test that needs to mount them in isolation.
 */

import { useState } from 'react';
import type { FormEvent } from 'react';

import {
  useDeliveryTrace,
  useEvalOutcomes,
  useHaltExecution,
  useRefreshSodexSymbols,
  useResumeExecution,
  useRetryAiNullSignals,
  useRotateWebhookSecret,
  useSetUserPaperTrade,
  useTriggerSignalCycle,
  useUnbindUserWallet,
} from '../../api/queries';
import {
  ActionSection,
  ConfirmButton,
  IdInput,
  ScopeRadios,
  type Scope,
} from '../../components/admin';
import { Button, Kicker } from '../../components/ui';
import { parsePositiveId } from '../../lib/parseId';
import {
  DeliveryTraceResultDisplay,
  EvalOutcomesResultDisplay,
  HaltResult,
  PaperTradeResult,
  ResumeResult,
  RetryAiResultDisplay,
  RotateResult,
  SymbolsRefreshResultDisplay,
  TriggerResult,
  UnbindResult,
} from './results';

// ===========================================================================
// ActionsPanel — composer of all 9 mutation sections.
// ===========================================================================

export function ActionsPanel({ adminKey }: { adminKey: string }) {
  return (
    <section className="border border-border-2 bg-bg-2 rounded-md p-4 space-y-4">
      <Kicker>Actions</Kicker>
      <TriggerCycleSection adminKey={adminKey} />
      <RetryAiSection adminKey={adminKey} />
      <EvalOutcomesSection adminKey={adminKey} />
      <RotateWebhookSecretSection adminKey={adminKey} />
      <PaperTradeSection adminKey={adminKey} />
      <UnbindWalletSection adminKey={adminKey} />
      <HaltExecutionSection adminKey={adminKey} />
      <ResumeExecutionSection adminKey={adminKey} />
      <SymbolsRefreshSection adminKey={adminKey} />
    </section>
  );
}

// ===========================================================================
// Existing 4 simple-mutation sections (cycle, retry-ai, eval, rotate).
// ===========================================================================

function TriggerCycleSection({ adminKey }: { adminKey: string }) {
  const m = useTriggerSignalCycle(adminKey);
  return (
    <ActionSection
      withDivider={false}
      title="Trigger signal cycle"
      description="Runs the same code path as the scheduled cron — ingest flows + news, run all 5 detectors, enrich with AI. Synchronous; may take ~10–60s."
      controls={
        <Button type="button" variant="primary" onClick={() => m.mutate()} disabled={m.isPending}>
          {m.isPending ? 'Running…' : 'Run cycle'}
        </Button>
      }
      error={m.error}
    >
      {m.data && <TriggerResult result={m.data} />}
    </ActionSection>
  );
}

function RetryAiSection({ adminKey }: { adminKey: string }) {
  const m = useRetryAiNullSignals(adminKey);
  return (
    <ActionSection
      title="Retry AI on stale signals"
      description={
        <>
          Re-runs OpenRouter on Signals with NULL{' '}
          <code className="font-mono">ai_analysis</code> (stranded by an earlier
          credit-out / quota / schema failure — the daily cycle never retries existing
          rows). Caps at 10 calls per click.
        </>
      }
      controls={
        <Button
          type="button"
          variant="primary"
          onClick={() => m.mutate(10)}
          disabled={m.isPending}
        >
          {m.isPending ? 'Retrying…' : 'Retry AI (10)'}
        </Button>
      }
      error={m.error}
    >
      {m.data && <RetryAiResultDisplay result={m.data} />}
    </ActionSection>
  );
}

function EvalOutcomesSection({ adminKey }: { adminKey: string }) {
  const m = useEvalOutcomes(adminKey);
  return (
    <ActionSection
      title="Evaluate outcomes now"
      description={
        <>
          Scores aged signals (≥72h old, with price + confidence) into{' '}
          <code className="font-mono">SignalOutcome</code> rows. The scheduled job runs
          hourly capped at <code className="font-mono">OUTCOME_EVAL_BATCH_LIMIT</code>{' '}
          (default 50); this button uses a smaller per-click cap of 20 to stay safely
          below SoSoValue's rate limit. Re-fire while{' '}
          <code className="font-mono">remaining &gt; 0</code> to drain a backlog.
        </>
      }
      controls={
        <Button
          type="button"
          variant="primary"
          onClick={() => m.mutate(20)}
          disabled={m.isPending}
        >
          {m.isPending ? 'Evaluating…' : 'Evaluate (20)'}
        </Button>
      }
      error={m.error}
    >
      {m.data && <EvalOutcomesResultDisplay result={m.data} />}
    </ActionSection>
  );
}

function RotateWebhookSecretSection({ adminKey }: { adminKey: string }) {
  const m = useRotateWebhookSecret(adminKey);
  return (
    <ActionSection
      title="Rotate Telegram webhook secret"
      description={
        <>
          Issues a fresh secret and pushes it to Telegram race-free. Mirror the new
          value into <code className="font-mono">TELEGRAM_WEBHOOK_SECRET</code> before
          the next container restart, or the boot-time value will overwrite it.
        </>
      }
      controls={
        <ConfirmButton
          idleLabel="Rotate…"
          confirmLabel="Confirm rotate"
          onConfirm={() => m.mutate()}
          pending={m.isPending}
        />
      }
      error={m.error}
    >
      {m.data && <RotateResult result={m.data} />}
    </ActionSection>
  );
}

// ===========================================================================
// New #186 sections — user mgmt, execution control, sodex ops.
// ===========================================================================

export function PaperTradeSection({ adminKey }: { adminKey: string }) {
  const m = useSetUserPaperTrade(adminKey);
  const [userIdRaw, setUserIdRaw] = useState('');
  const userId = parsePositiveId(userIdRaw);
  const onClick = (paperTrade: boolean) => {
    if (userId === null) return;
    m.mutate({ userId, paperTrade });
  };
  return (
    <ActionSection
      title="Flip user paper-trade"
      description={
        <>
          Set <code className="font-mono">User.paper_trade</code> on a specific user.
          Existing orders are NOT mutated — only future{' '}
          <code className="font-mono">prepare_order</code> calls inherit the new value.
          Idempotent. Pairs with the user-side request-live flow (#185).
        </>
      }
      controls={
        <div className="flex flex-wrap items-center gap-2 min-w-[260px]">
          <IdInput
            value={userIdRaw}
            onChange={setUserIdRaw}
            placeholder="user id"
            ariaLabel="user id"
          />
          <Button
            type="button"
            variant="primary"
            onClick={() => onClick(true)}
            disabled={userId === null || m.isPending}
          >
            Set TRUE
          </Button>
          <Button
            type="button"
            variant="secondary"
            onClick={() => onClick(false)}
            disabled={userId === null || m.isPending}
          >
            Set FALSE
          </Button>
        </div>
      }
      error={m.error}
    >
      {m.data && <PaperTradeResult result={m.data} />}
    </ActionSection>
  );
}

export function UnbindWalletSection({ adminKey }: { adminKey: string }) {
  const m = useUnbindUserWallet(adminKey);
  const [userIdRaw, setUserIdRaw] = useState('');
  const userId = parsePositiveId(userIdRaw);
  return (
    <ActionSection
      title="Unbind user wallet"
      description={
        <>
          Clears <code className="font-mono">wallet_address</code> +{' '}
          <code className="font-mono">sodex_account_id</code> + both venue api-key
          names. Destructive: in-flight orders for the unbound wallet are stranded
          (nonce-expiry reaper handles them). Idempotent on re-run. Use for wallet-loss
          recovery (#78.7).
        </>
      }
      controls={
        <div className="flex flex-wrap items-center gap-2 min-w-[260px]">
          <IdInput
            value={userIdRaw}
            onChange={setUserIdRaw}
            placeholder="user id"
            ariaLabel="user id"
            disabled={m.isPending}
          />
          <ConfirmButton
            idleLabel="Unbind…"
            confirmLabel={`Confirm unbind #${userId}`}
            onConfirm={() => userId !== null && m.mutate(userId)}
            disabled={userId === null}
            pending={m.isPending}
          />
        </div>
      }
      error={m.error}
    >
      {m.data && <UnbindResult result={m.data} />}
    </ActionSection>
  );
}

export function HaltExecutionSection({ adminKey }: { adminKey: string }) {
  const m = useHaltExecution(adminKey);
  const [scope, setScope] = useState<Scope>('global');
  const [userIdRaw, setUserIdRaw] = useState('');
  const [reason, setReason] = useState('');
  const userId = scope === 'user' ? parsePositiveId(userIdRaw) : null;
  const inputValid =
    reason.trim().length > 0 && (scope === 'global' || userId !== null);
  return (
    <ActionSection
      title={
        <>
          Halt execution{' '}
          <span className="text-warn text-[11px] uppercase tracking-[0.1em]">
            destructive
          </span>
        </>
      }
      description={
        <>
          Trips the <code className="font-mono">manual</code> circuit breaker — global
          (every prepare 503s) or per-user. Idempotent: re-halting an active scope
          returns the existing breaker's id + details without inserting a duplicate.
          Pairs with Resume below.
        </>
      }
      controls={
        <div className="flex flex-col gap-2 min-w-[280px]">
          <ScopeRadios
            name="halt-scope"
            scope={scope}
            onChange={setScope}
            disabled={m.isPending}
            perUserInput={
              <IdInput
                value={userIdRaw}
                onChange={setUserIdRaw}
                placeholder="user id"
                ariaLabel="user id"
                disabled={m.isPending}
                width="w-24"
              />
            }
          />
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value.slice(0, 500))}
            maxLength={500}
            placeholder="reason (required, max 500 chars)"
            aria-label="halt reason"
            className="w-full bg-bg-3 text-text-1 border border-border-3 rounded-[5px] px-3 py-2 text-[13px] font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            disabled={m.isPending}
          />
          <ConfirmButton
            idleLabel="Halt…"
            confirmLabel={`Confirm halt ${scope === 'global' ? '(global)' : `user #${userId}`}`}
            onConfirm={() => {
              if (!inputValid) return;
              m.mutate({ reason: reason.trim(), userId });
            }}
            disabled={!inputValid}
            pending={m.isPending}
          />
        </div>
      }
      error={m.error}
    >
      {m.data && <HaltResult result={m.data} />}
    </ActionSection>
  );
}

export function ResumeExecutionSection({ adminKey }: { adminKey: string }) {
  const m = useResumeExecution(adminKey);
  const [scope, setScope] = useState<Scope>('global');
  const [userIdRaw, setUserIdRaw] = useState('');
  const userId = scope === 'user' ? parsePositiveId(userIdRaw) : null;
  const inputValid = scope === 'global' || userId !== null;
  return (
    <ActionSection
      title="Resume execution"
      description={
        <>
          Resolves the <code className="font-mono">manual</code> circuit breaker for a
          scope. Global resume does NOT clear per-user breakers — operator must call
          once per scope. <code className="font-mono">rowcount=0</code> response means
          nothing was active to resume (200, not an error).
        </>
      }
      controls={
        <div className="flex flex-col gap-2 min-w-[280px]">
          <ScopeRadios
            name="resume-scope"
            scope={scope}
            onChange={setScope}
            disabled={m.isPending}
            perUserInput={
              <IdInput
                value={userIdRaw}
                onChange={setUserIdRaw}
                placeholder="user id"
                ariaLabel="user id"
                disabled={m.isPending}
                width="w-24"
              />
            }
          />
          <Button
            type="button"
            variant="primary"
            onClick={() => inputValid && m.mutate(userId)}
            disabled={!inputValid || m.isPending}
          >
            {m.isPending ? 'Resuming…' : 'Resume'}
          </Button>
        </div>
      }
      error={m.error}
    >
      {m.data && <ResumeResult result={m.data} />}
    </ActionSection>
  );
}

export function SymbolsRefreshSection({ adminKey }: { adminKey: string }) {
  const m = useRefreshSodexSymbols(adminKey);
  return (
    <ActionSection
      title="Refresh SoDEX symbols"
      description={
        <>
          Force-refreshes the cached <code className="font-mono">sodex_symbols</code>{' '}
          table ahead of the daily 04:00 UTC cron. Useful when SoDEX lists a new pair
          and you don't want to wait. Backend returns 503 if SoDEX HTTP clients aren't
          attached (scheduler disabled).
        </>
      }
      controls={
        <Button
          type="button"
          variant="primary"
          onClick={() => m.mutate()}
          disabled={m.isPending}
        >
          {m.isPending ? 'Refreshing…' : 'Refresh now'}
        </Button>
      }
      error={m.error}
    >
      {m.data && <SymbolsRefreshResultDisplay result={m.data} />}
    </ActionSection>
  );
}

// ===========================================================================
// DeliveryTracePanel — read-only debug surface, distinct visual grouping.
// ===========================================================================

export function DeliveryTracePanel({ adminKey }: { adminKey: string }) {
  // `submittedId` is what's actually queried; the input is only the
  // pending entry. Without this split, every keystroke would refire
  // the query — wasteful + flashes spinners.
  const [signalIdRaw, setSignalIdRaw] = useState('');
  const [submittedId, setSubmittedId] = useState<number | null>(null);
  const parsedId = parsePositiveId(signalIdRaw);
  const query = useDeliveryTrace(adminKey, submittedId);
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setSubmittedId(parsedId);
  };
  return (
    <section className="border border-border-2 bg-bg-2 rounded-md p-4 space-y-4">
      <Kicker>Delivery trace</Kicker>
      <form onSubmit={onSubmit}>
        <ActionSection
          withDivider={false}
          title="Look up by signal id"
          description={
            <>
              Diagnostic: who matched this signal, who didn't, why, and what state each
              <code className="font-mono"> SignalDelivery </code>row is in. Answers
              &quot;did this signal reach anyone, and if not, why?&quot; without dropping
              to SQL.
            </>
          }
          controls={
            <div className="flex items-center gap-2">
              <IdInput
                value={signalIdRaw}
                onChange={setSignalIdRaw}
                placeholder="signal id"
                ariaLabel="signal id"
                width="w-32"
              />
              <Button type="submit" variant="primary" disabled={parsedId === null}>
                {query.isFetching && submittedId === parsedId ? 'Loading…' : 'Trace'}
              </Button>
            </div>
          }
          error={query.error}
        >
          {query.data && <DeliveryTraceResultDisplay result={query.data} />}
        </ActionSection>
      </form>
    </section>
  );
}
