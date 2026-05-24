/**
 * `<ConfirmButton>` — two-step destructive-action button (#187).
 *
 * State machine:
 *   idle      → user clicks `idleLabel` button         → confirming
 *   confirming → user clicks `confirmLabel` button     → fires `onConfirm()`, returns to idle
 *   confirming → user clicks Cancel                    → returns to idle
 *
 * Used by destructive admin actions (rotate webhook secret, unbind wallet,
 * halt execution) to prevent fat-finger fires. Replaces ~30 lines of
 * inline state machine per call site.
 *
 * Disable semantics:
 *   `disabled`  → idle button + Confirm button both blocked. Used when
 *                 the parent's input is invalid (no id, empty reason).
 *                 Cancel stays enabled so the operator can exit the
 *                 confirming state.
 *   `pending`   → idle + Confirm + Cancel ALL blocked. Used while the
 *                 parent's mutation is in flight; cancelling mid-network
 *                 is a race we don't expose.
 */

import { useState } from 'react';

import { Button } from '../ui';

interface Props {
  /** Text on the initial idle button. e.g. "Unbind…" */
  idleLabel: string;
  /** Text on the confirming-state primary button. e.g. "Confirm unbind #42" */
  confirmLabel: string;
  /** Fires when the operator clicks Confirm. */
  onConfirm: () => void;
  /** Disables the action — applies to BOTH the idle button AND the
   *  in-confirming Confirm button. Without the second gate, a user who
   *  entered confirming state with valid input then cleared the input
   *  could click Confirm and see no mutation fire — silent no-op UX. */
  disabled?: boolean;
  /** True while the parent's mutation is in flight — also disables the
   *  idle button so a user re-clicking before the result lands can't
   *  re-enter confirming state. */
  pending?: boolean;
}

/** Two-step confirm flow. On Confirm click, fires `onConfirm` AND
 *  flips back to idle synchronously — so during the parent's pending
 *  phase the user sees the idle button (disabled). This matches the
 *  pre-#187 inline pattern and keeps the UI from showing two
 *  disabled-buttons mid-flight. */
export function ConfirmButton({
  idleLabel,
  confirmLabel,
  onConfirm,
  disabled = false,
  pending = false,
}: Props) {
  const [confirming, setConfirming] = useState(false);

  if (!confirming) {
    return (
      <Button
        type="button"
        variant="secondary"
        onClick={() => setConfirming(true)}
        disabled={disabled || pending}
      >
        {idleLabel}
      </Button>
    );
  }

  return (
    <div className="flex gap-2">
      <Button
        type="button"
        variant="primary"
        onClick={() => {
          // Belt — `disabled` + `pending` ALSO disable this button below,
          // but a synchronous-double-click within the same microtask
          // (before React re-renders after the first click) would slip
          // past the disabled prop. The early-return here defends
          // against that narrow race so a not-idempotent mutation (e.g.,
          // rotate-secret) can't fire twice from one Confirm click —
          // AND so an invalid-input mid-confirm doesn't fire a no-op
          // mutation that the parent's onConfirm would short-circuit.
          if (disabled || pending) return;
          onConfirm();
          setConfirming(false);
        }}
        disabled={disabled || pending}
      >
        {confirmLabel}
      </Button>
      <Button
        type="button"
        variant="ghost"
        onClick={() => setConfirming(false)}
        disabled={pending}
      >
        Cancel
      </Button>
    </div>
  );
}
