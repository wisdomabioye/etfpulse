import { useParams } from 'react-router-dom';

import { ApiError } from '../api/client';
import { useSignal } from '../api/queries';
import { SignalDetailBody } from '../components/signal-detail/SignalDetailBody';
import { DetailLoading, NotFound } from '../components/signal-detail/SignalDetailStates';
import { Button, EmptyState } from '../components/ui';

/**
 * /signals/:id — why → R:R → outcome → execute (R7 reskin of the prototype's
 * detail screen). Preserves the real `useSignal` wiring, every null state, and
 * the SIG2X "⚡ Execute this signal" CTA (link → `/execute?signal_id=…`). The
 * data-heavy sections (outcome, trigger data, news, factor breakdown) reuse
 * their existing, tested components under reskinned section labels.
 */
export function SignalDetail() {
  const { id: idParam } = useParams<{ id: string }>();
  const id = idParam && /^\d+$/.test(idParam) ? Number(idParam) : undefined;
  const query = useSignal(id);

  if (id === undefined) return <NotFound />;

  return (
    <article className="max-w-[840px] mx-auto px-6 pt-8 pb-12">
      {query.isLoading ? (
        <DetailLoading />
      ) : query.isError ? (
        query.error instanceof ApiError && query.error.status === 404 ? (
          <NotFound />
        ) : (
          <EmptyState
            title="Couldn't load signal."
            hint="Check your connection and retry."
            action={
              <Button variant="secondary" size="sm" onClick={() => query.refetch()}>
                Retry
              </Button>
            }
          />
        )
      ) : query.data ? (
        <SignalDetailBody signal={query.data} />
      ) : null}
    </article>
  );
}
