import type { FormEvent } from 'react';

import { Button } from '../ui';

interface AdminKeyFormProps {
  /** Current input value the operator is typing. */
  keyInput: string;
  /** Setter wired to the input field. */
  onInputChange: (next: string) => void;
  /** The key currently in use (after Unlock/Reload). Empty string =
   *  not unlocked yet. Controls the Submit button label + the "Clear"
   *  button visibility. */
  activeKey: string;
  /** Called when the operator submits the form. Caller decides what
   *  to do with `keyInput` (persist + activate, typically). */
  onSubmit: () => void;
  /** Called when the operator clears the active key. Caller resets
   *  storage + local state. */
  onClear: () => void;
}

/**
 * Admin-key gate form. Pure presentation — state ownership stays with
 * the page so each page can react to unlock/clear independently. The
 * form fires `onSubmit` instead of leaking a FormEvent so callers
 * don't have to remember to call `.preventDefault()`.
 *
 * Shared between `/admin` and `/admin/backtest` (PR P2.4); any future
 * admin surface uses this too. Authoring a new admin page should never
 * involve re-implementing the input + Unlock/Clear flow.
 */
export function AdminKeyForm({
  keyInput,
  onInputChange,
  activeKey,
  onSubmit,
  onClear,
}: AdminKeyFormProps) {
  const handle = (e: FormEvent) => {
    e.preventDefault();
    onSubmit();
  };

  return (
    <form
      onSubmit={handle}
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
          onChange={(e) => onInputChange(e.target.value)}
          placeholder="X-Admin-Key header value"
          className="w-full bg-bg-3 text-text-1 border border-border-3 rounded-[5px] px-3 py-2 text-[13px] font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </label>
      <Button type="submit" variant="primary">
        {activeKey ? 'Reload' : 'Unlock'}
      </Button>
      {activeKey && (
        <Button type="button" variant="ghost" onClick={onClear}>
          Clear key
        </Button>
      )}
    </form>
  );
}
