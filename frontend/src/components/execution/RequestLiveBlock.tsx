/**
 * Request-live-trading breadcrumb (#185). Paper-trade users get a note + a
 * "Request live trading" form that pings the operator; the operator is the
 * only switch that flips `paper_trade`.
 */

import { useRef, useState } from 'react';

import { ApiError } from '../../api/client';
import { useRequestLive } from '../../hooks/useExecution';
import { Button, Callout } from '../ui';

export function RequestLiveBlock() {
  const mutation = useRequestLive();
  const [note, setNote] = useState('');
  const [showForm, setShowForm] = useState(false);
  // Synchronous in-flight guard — `useMutation`'s `isPending` lands on
  // React's schedule; a fast double-click can slip past `disabled`.
  const inFlightRef = useRef(false);

  async function handleSubmit() {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      await mutation.mutateAsync({ note: note.trim() || undefined });
      setShowForm(false);
      setNote('');
    } catch {
      // mutation.error carries the ApiError for inline render below.
    } finally {
      inFlightRef.current = false;
    }
  }

  if (mutation.isSuccess && mutation.data) {
    return <Callout tone="pos">{mutation.data.message}</Callout>;
  }

  return (
    <div className="rounded-md border border-line-2 bg-bg-2 p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-t2 text-xs flex-1 min-w-[200px]">
          You&apos;re in paper-trade mode. Orders use simulated fills — no real funds move.
          Ready to go live? Ask the operator to flip you over.
        </p>
        {!showForm && (
          <Button variant="outline" size="sm" onClick={() => setShowForm(true)}>
            Request live trading →
          </Button>
        )}
      </div>
      {showForm && (
        <div className="space-y-2 pt-1">
          <label className="block text-t2 text-xs">
            Optional note for the operator (max 500 chars):
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value.slice(0, 500))}
              maxLength={500}
              rows={2}
              className="mt-1 w-full rounded-md bg-bg-1 border border-line-2 text-t1 text-xs p-2 resize-y"
              placeholder="e.g. ran one paper order, ready for live"
            />
          </label>
          {mutation.isError && (
            <div className="text-warn text-xs">
              {mutation.error instanceof ApiError && mutation.error.status === 429
                ? `Already sent recently. ${mutation.error.detail.replace(/^request_live_cooldown: /, '')}`
                : "Couldn't send. Please contact the operator directly."}
            </div>
          )}
          <div className="flex gap-2">
            <Button variant="secondary" size="sm" onClick={handleSubmit} disabled={mutation.isPending}>
              {mutation.isPending ? 'Sending…' : 'Send request'}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setShowForm(false);
                setNote('');
                mutation.reset();
              }}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
