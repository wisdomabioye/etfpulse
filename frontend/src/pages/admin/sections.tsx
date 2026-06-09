/**
 * Admin action sections (#187 split).
 *
 * Each section owns one mutation hook + minimal local form state + uses
 * the `ActionSection` / `ConfirmButton` / `IdInput` / `ScopeRadios`
 * primitives from `components/admin/`. Result rendering is delegated to
 * sibling `results.tsx`.
 *
 * The section components themselves live in two sibling modules to keep
 * every file under the size cap:
 *   - `sections-pipeline.tsx`  — the 4 simple-mutation sections
 *     (`TriggerCycleSection`, `RetryAiSection`, `EvalOutcomesSection`,
 *     `RotateWebhookSecretSection`).
 *   - `sections-execution.tsx` — the #186 user-mgmt / execution-control /
 *     sodex-ops sections + `DeliveryTracePanel`.
 *
 * `ActionsPanel` (here) composes the 9 mutation sections in a single
 * `<section>`. `DeliveryTracePanel` is a separate `<section>` (different
 * visual grouping — read-only debug surface, not an action).
 *
 * Public exports: `ActionsPanel` (defined here), plus the execution
 * sections re-exported below so existing consumers keep working:
 * `PaperTradeSection`, `UnbindWalletSection`, `HaltExecutionSection`,
 * `ResumeExecutionSection`, `SymbolsRefreshSection`, `DeliveryTracePanel`.
 */

import { Kicker } from '../../components/ui';
import {
  EvalOutcomesSection,
  RetryAiSection,
  RotateWebhookSecretSection,
  TriggerCycleSection,
} from './sections-pipeline';
import {
  HaltExecutionSection,
  PaperTradeSection,
  ResumeExecutionSection,
  SymbolsRefreshSection,
  UnbindWalletSection,
} from './sections-execution';

// ===========================================================================
// ActionsPanel — composer of all 9 mutation sections.
// ===========================================================================

export function ActionsPanel({ adminKey }: { adminKey: string }) {
  return (
    <section className="border border-line-2 bg-bg-2 rounded-md p-4 space-y-4">
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
// Re-exports — keep existing consumer imports from `./sections` valid.
// ===========================================================================

export {
  PaperTradeSection,
  UnbindWalletSection,
  HaltExecutionSection,
  ResumeExecutionSection,
  SymbolsRefreshSection,
  DeliveryTracePanel,
} from './sections-execution';
