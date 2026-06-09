/**
 * Admin delivery-trace debug surface (#187 split).
 *
 * `DeliveryTracePanel` is a read-only debug `<section>` (distinct visual
 * grouping from the action sections — not a mutation). Lives in its own
 * module to keep every file under the size cap; re-exported through
 * `sections-execution.tsx` so consumer import paths stay unchanged.
 */

import { useState } from 'react';
import type { FormEvent } from 'react';

import { useDeliveryTrace } from '../../api/queries';
import { ActionSection, IdInput } from '../../components/admin';
import { Button, Kicker } from '../../components/ui';
import { parsePositiveId } from '../../lib/parseId';
import { DeliveryTraceResultDisplay } from './results';

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
    <section className="border border-line-2 bg-bg-2 rounded-md p-4 space-y-4">
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
