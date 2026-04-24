import { Link, useParams } from 'react-router-dom';
import { useSignal } from '../api/queries';
import {
  AssetBadge,
  ConfidenceBadge,
  OutcomeCard,
  SignalTypeBadge,
  SuggestedActionPanel,
  TriggerDataTable,
} from '../components/signals';
import {
  Button,
  Callout,
  CTABanner,
  EmptyState,
  SectionLabel,
  Skeleton,
} from '../components/ui';
import { ApiError } from '../api/client';
import { formatAgo, truncateFingerprint } from '../lib/format';

/**
 * /signals/:id — matches wireframe `src/screen-detail.jsx`.
 *
 * Article max-w-[780px], single reading column. Null handling:
 *   - invalid ID            → "Signal not found"
 *   - 404 from API          → "Signal not found"
 *   - 5xx / network         → "Couldn't load" + retry
 *   - ai_analysis === null  → headline/panel/reasoning/risks hidden;
 *                             trigger data + outcome + CTA still render
 *   - outcome === null      → OutcomeCard renders "Pending · evaluates in Xh"
 */
export function SignalDetail() {
  const { id: idParam } = useParams<{ id: string }>();
  const id = idParam && /^\d+$/.test(idParam) ? Number(idParam) : undefined;

  const query = useSignal(id);

  if (id === undefined) {
    return <NotFound />;
  }

  return (
    <article className="max-w-[780px] mx-auto px-6 sm:px-8 pt-8 pb-16">
      <Link
        to="/signals"
        className="inline-block font-mono text-[11px] text-text-3 hover:text-text-1 mb-6"
      >
        ← all signals
      </Link>

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
        <Body signal={query.data} />
      ) : null}
    </article>
  );
}

function Body({ signal: s }: { signal: import('../api/types').SignalDetail }) {
  const analysis = s.ai_analysis;

  return (
    <>
      <div className="flex items-center gap-2 mb-3.5">
        <AssetBadge asset={s.asset} />
        <SignalTypeBadge type={s.signal_type} />
        <span className="flex-1" />
        <ConfidenceBadge value={s.confidence} size="lg" />
      </div>

      {analysis ? (
        <h1
          className="mt-2.5 mb-3.5 text-[32px] font-semibold text-text-1 leading-[1.15]"
          style={{ letterSpacing: '-0.025em', textWrap: 'balance' }}
        >
          {analysis.headline}
        </h1>
      ) : (
        <div className="mt-2.5 mb-3.5">
          <EmptyState
            title="AI analysis unavailable."
            hint="Enrichment failed when this signal was generated. The trigger data below is still intact."
          />
        </div>
      )}

      <div className="font-mono text-[12px] text-text-3 mb-8 break-words">
        Signal #{s.id} · Generated {formatAgo(s.created_at)} · Alerted to{' '}
        {s.alerted_to.toLocaleString()} users · fp:{truncateFingerprint(s.fingerprint)}
      </div>

      {analysis && <SuggestedActionPanel analysis={analysis} />}

      {analysis && analysis.reasoning.length > 0 && (
        <section className="mt-10">
          <SectionLabel n="01">AI Reasoning</SectionLabel>
          <ul className="m-0 p-0 list-none">
            {analysis.reasoning.map((r, i) => (
              <li
                key={i}
                className={`grid gap-3 py-3.5 items-baseline ${
                  i === 0 ? 'border-t border-border-2' : 'border-t border-border-1'
                }`}
                style={{ gridTemplateColumns: '28px 1fr' }}
              >
                <span className="font-mono text-[11px] text-text-4 pt-0.5">
                  {String(i + 1).padStart(2, '0')}
                </span>
                <span
                  className="text-[15px] leading-[1.6] text-text-1"
                  style={{ textWrap: 'pretty' }}
                >
                  {r}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="mt-10">
        <SectionLabel n="02">Trigger Data</SectionLabel>
        <TriggerDataTable data={s.trigger_data} />
      </section>

      {analysis && analysis.risks.length > 0 && (
        <section className="mt-10">
          <SectionLabel n="03">Risks</SectionLabel>
          <div className="flex flex-col gap-2">
            {analysis.risks.map((r, i) => (
              <Callout key={i} tone="warn">
                {r}
              </Callout>
            ))}
          </div>
        </section>
      )}

      <section className="mt-10">
        <SectionLabel n="04">Outcome</SectionLabel>
        <OutcomeCard outcome={s.outcome} expiresAt={s.expires_at} />
      </section>

      <CTABanner
        className="mt-12"
        title="Continue the discussion"
        hint={`Deep-linked to signal #${s.id} in Telegram.`}
        action={
          <Button as="a" href="#" variant="primary" size="md">
            Discuss on Telegram
            <span className="font-mono opacity-70">↗</span>
          </Button>
        }
      />
    </>
  );
}

function DetailLoading() {
  return (
    <div className="flex flex-col gap-4">
      <Skeleton className="h-6 w-48" />
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-4 w-72" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}

function NotFound() {
  return (
    <EmptyState
      title="Signal not found."
      hint="It may have been removed, or the link is incorrect."
      action={
        <Button as="link" to="/signals" variant="secondary" size="sm">
          Back to feed
        </Button>
      }
    />
  );
}
