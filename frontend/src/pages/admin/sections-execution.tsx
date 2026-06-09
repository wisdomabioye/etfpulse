/**
 * Admin execution + sodex-ops mutation sections (#187 split).
 *
 * The #186 sections — user mgmt, execution control, sodex ops — plus the
 * read-only `DeliveryTracePanel` debug surface. These are the sections
 * covered by component tests in `Admin.test.tsx`
 * (`PaperTradeSection`, `UnbindWalletSection`, `HaltExecutionSection`,
 * `ResumeExecutionSection`, `SymbolsRefreshSection`) plus `DeliveryTracePanel`.
 */

import { useState } from 'react';

import {
  useHaltExecution,
  useRefreshSodexSymbols,
  useResumeExecution,
  useSetUserPaperTrade,
  useUnbindUserWallet,
} from '../../api/queries';
import {
  ActionSection,
  ConfirmButton,
  IdInput,
  ScopeRadios,
  type Scope,
} from '../../components/admin';
import { Button } from '../../components/ui';
import { parsePositiveId } from '../../lib/parseId';
import {
  HaltResult,
  PaperTradeResult,
  ResumeResult,
  SymbolsRefreshResultDisplay,
  UnbindResult,
} from './results';

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
            className="w-full bg-bg-3 text-t1 border border-line-3 rounded-[5px] px-3 py-2 text-[13px] font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-acc disabled:opacity-50"
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
// Defined in `sections-trace.tsx`; re-exported here so consumer imports
// from `./sections` (via `sections.tsx`) keep resolving unchanged.
// ===========================================================================

export { DeliveryTracePanel } from './sections-trace';
