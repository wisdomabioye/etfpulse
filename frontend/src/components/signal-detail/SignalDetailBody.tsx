import { Link } from 'react-router-dom';

import type { SignalDetail as SignalDetailType } from '../../api/types';
import { Breadcrumb } from '../layout';
import { RRBar } from '../charts';
import { NewsContextSection, SpotAtSignal, TriggerDataTable } from '../signals';
import { ConfirmationSection, OutcomeSection } from '../signal';
import {
  ActionTag,
  AssetBadge,
  Button,
  Callout,
  Card,
  ConfidenceBadge,
  ConfirmationPips,
  DetectorBadge,
  EmptyState,
  SectionLabel,
} from '../ui';
import { actionMeta } from '../../lib/colors';
import { colorMix, cssVar } from '../../lib/colorMix';
import { formatAbsUtc, formatAgo, formatPrice, truncateFingerprint } from '../../lib/format';
import { TELEGRAM_GROUP_URL } from '../../lib/links';
import { isExecutableSignal, signalExecuteHref } from '../../lib/signalExecute';

export function SignalDetailBody({ signal: s }: { signal: SignalDetailType }) {
  const analysis = s.ai_analysis;
  const showNews = Array.isArray(s.trigger_data.news_context);

  const action = analysis ? actionMeta(analysis.suggested_action) : null;
  const direction: 'long' | 'short' | null = analysis
    ? analysis.suggested_action === 'consider long'
      ? 'long'
      : analysis.suggested_action === 'consider short'
        ? 'short'
        : null
    : null;
  const hasLevels =
    analysis?.entry_price != null && analysis.stop_price != null && analysis.target_price != null;

  return (
    <>
      <Breadcrumb items={[{ label: 'Signals', path: '/signals' }, { label: `#${s.id}` }]} />

      {/* header */}
      <div className="flex items-center gap-2 flex-wrap mb-4">
        <AssetBadge asset={s.asset} />
        <DetectorBadge type={s.signal_type} />
        {analysis && <ActionTag action={analysis.suggested_action} />}
        {s.confirmation_score !== null && (
          <span className="font-mono text-[11px] text-t3 inline-flex items-center gap-1.5">
            conf <ConfirmationPips value={s.confirmation_score} /> {s.confirmation_score}/3
          </span>
        )}
        <span className="flex-1" />
        {s.confidence !== null && <ConfidenceBadge value={s.confidence} size="lg" />}
      </div>

      {analysis ? (
        <h1
          className="text-[30px] font-semibold leading-[1.18] mt-1 mb-3.5"
          style={{ letterSpacing: '-0.025em', textWrap: 'balance' }}
        >
          {analysis.headline}
        </h1>
      ) : (
        <div className="mt-1 mb-3.5">
          <EmptyState
            title="AI analysis unavailable."
            hint="Enrichment failed when this signal was generated. The trigger data below is still intact."
          />
        </div>
      )}

      <div className="font-mono text-[11.5px] text-t3 mb-6 flex flex-wrap gap-x-3.5 gap-y-1">
        <span>Signal #{s.id}</span>
        <span>·</span>
        <span>{formatAgo(s.created_at)}</span>
        <span>·</span>
        <span title={formatAbsUtc(s.created_at)}>{s.signal_date}</span>
        <span>·</span>
        <span>alerted to {s.alerted_to.toLocaleString()}</span>
        <span className="text-t4">· fp:{truncateFingerprint(s.fingerprint)}</span>
      </div>

      {/* primary action panel — the loop's hinge */}
      {action && (
        <div
          className="flex gap-3 items-center flex-wrap p-4 rounded-lg mb-6 border"
          style={{
            background: colorMix(action.tone, 6, cssVar('--bg-2')),
            borderColor: colorMix(action.tone, 26, cssVar('--line-2')),
            borderLeft: `3px solid ${cssVar(action.tone)}`,
          }}
        >
          <div className="flex-1 min-w-[200px] flex gap-4 items-baseline">
            <div>
              <div className="font-mono text-[9.5px] text-t4 tracking-[0.1em] uppercase">Direction</div>
              <div className="text-[17px] font-semibold" style={{ color: cssVar(action.tone) }}>
                {action.full}
              </div>
            </div>
            {analysis?.time_horizon && (
              <div>
                <div className="font-mono text-[9.5px] text-t4 tracking-[0.1em] uppercase">Horizon</div>
                <div className="text-[17px] font-semibold capitalize">{analysis.time_horizon}</div>
              </div>
            )}
          </div>
          {isExecutableSignal(s) && (
            <Button
              as="link"
              to={signalExecuteHref(s.id)}
              variant="primary"
              size="lg"
              icon={<span aria-hidden>⚡</span>}
            >
              Execute this signal
            </Button>
          )}
        </div>
      )}

      <SpotAtSignal price={s.price_at_creation} source={s.price_source} />

      {/* R:R viz */}
      {hasLevels && (
        <Card className="mb-6 mt-6">
          <RRBar
            entry={analysis!.entry_price as number}
            stop={analysis!.stop_price as number}
            target={analysis!.target_price as number}
          />
          <div className="mt-2 pt-3.5 border-t border-line-1 flex gap-5 flex-wrap font-mono text-[11px] text-t3">
            <span>
              price at signal <span className="text-t1">{formatPrice(s.price_at_creation)}</span>
            </span>
            {s.price_source && (
              <span>
                source <span className="text-t1">{s.price_source}</span>
              </span>
            )}
            {s.expires_at && (
              <span>
                expires <span className="text-warn">{formatAbsUtc(s.expires_at).slice(0, 16)}</span>
              </span>
            )}
          </div>
        </Card>
      )}

      {/* reasoning + risks */}
      {analysis && (analysis.reasoning.length > 0 || analysis.risks.length > 0) && (
        <div className="grid grid-cols-1 md:grid-cols-[1.4fr_1fr] gap-5 mb-6">
          {analysis.reasoning.length > 0 && (
            <div>
              <SectionLabel>AI Reasoning</SectionLabel>
              <ol className="m-0 p-0 list-none">
                {analysis.reasoning.map((r, i) => (
                  <li
                    key={i}
                    className={`grid gap-3 py-3.5 ${i ? 'border-t border-line-1' : 'border-t border-line-2'}`}
                    style={{ gridTemplateColumns: '26px 1fr' }}
                  >
                    <span className="font-mono tabular-nums text-[11px] text-t4">
                      {String(i + 1).padStart(2, '0')}
                    </span>
                    <span className="text-[14px] leading-[1.6]" style={{ textWrap: 'pretty' }}>
                      {r}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}
          {analysis.risks.length > 0 && (
            <div>
              <SectionLabel>Risks</SectionLabel>
              <div className="flex flex-col gap-2">
                {analysis.risks.map((r, i) => (
                  <Callout key={i} tone="warn">
                    {r}
                  </Callout>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* confirmation — prototype titled card + 3-col factor tiles */}
      {s.confirmation_score !== null && s.factor_votes !== null && (
        <section className="mb-6">
          <ConfirmationSection score={s.confirmation_score} votes={s.factor_votes} />
        </section>
      )}

      {showNews && (
        <section className="mb-6">
          <SectionLabel>News Context</SectionLabel>
          <NewsContextSection triggerData={s.trigger_data} />
        </section>
      )}

      <section className="mb-6">
        <SectionLabel>Trigger Data</SectionLabel>
        <TriggerDataTable data={s.trigger_data} />
      </section>

      <section className="mb-8">
        <SectionLabel>Outcome</SectionLabel>
        <OutcomeSection
          outcome={s.outcome}
          expiresAt={s.expires_at}
          createdAt={s.created_at}
          priceAtCreation={s.price_at_creation}
          direction={direction}
        />
      </section>

      {/* cross-links */}
      <div className="flex gap-3 flex-wrap">
        <Link
          to="/regime"
          className="flex-1 min-w-[200px] p-4 bg-bg-2 border border-line-2 rounded-md hover:border-acc-line transition-colors"
        >
          <span className="font-mono text-[9.5px] text-t4 tracking-[0.1em] uppercase">
            Regime context
          </span>
          <span className="block text-[13px] mt-1.5">View current market regime →</span>
        </Link>
        <a
          href={TELEGRAM_GROUP_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="flex-1 min-w-[200px] p-4 bg-bg-2 border border-line-2 rounded-md hover:border-acc-line transition-colors"
        >
          <span className="font-mono text-[9.5px] text-t4 tracking-[0.1em] uppercase">
            Also delivered to
          </span>
          <span className="block text-[13px] mt-1.5">
            Telegram · {s.alerted_to.toLocaleString()} subscribers ↗
          </span>
        </a>
      </div>
    </>
  );
}
