import { ApiError } from '../api/client';
import { useRegime } from '../api/queries';
import type { RegimeResponse } from '../api/types';
import { RegimeIndicator } from '../components/charts';
import { Breadcrumb, Page } from '../components/layout';
import { RegimeHistoryStrip } from '../components/regime/RegimeHistoryStrip';
import { Button, Card, EmptyState, Kicker, SectionHeader, Skeleton } from '../components/ui';
import { REGIMES } from '../lib/constants';
import { regimeColorToken } from '../lib/colors';
import { colorMix, cssVar } from '../lib/colorMix';
import { formatPosture, formatRegime } from '../lib/format';
import { asNumber, readContributions, readDominance } from '../lib/regimeReasoning';

const signed = (n: number) => `${n >= 0 ? '+' : ''}${n}`;

/**
 * /regime — market-state instrument (rebuilt to the prototype): gauge hero,
 * metric band, component-reasoning + score-contribution columns, and the
 * 8-day history strip. Every number is real, sourced from the snapshot's
 * `reasoning` JSONB + the new `/regime/history` endpoint. (Per-sector flow
 * bars from the prototype have no API data — replaced by the real directional
 * score contributions; the classifier doesn't expose per-sector flow.)
 */
export function Regime() {
  const query = useRegime();
  return (
    <Page>
      <Breadcrumb items={[{ label: 'ETFPulse', path: '/' }, { label: 'Regime' }]} />
      <Kicker>Market-state instrument</Kicker>
      <h1 className="text-[30px] font-semibold tracking-[-0.025em] mt-3 mb-6">Current regime</h1>

      {query.isLoading ? (
        <RegimeLoading />
      ) : query.isError ? (
        query.error instanceof ApiError && query.error.status === 503 ? (
          <EmptyState
            title="Regime not yet classified."
            hint="The classifier runs as part of each cycle. Check back shortly."
          />
        ) : (
          <EmptyState
            title="Couldn't load regime."
            hint="Check your connection and retry."
            action={
              <Button variant="secondary" size="sm" onClick={() => query.refetch()}>
                Retry
              </Button>
            }
          />
        )
      ) : query.data ? (
        <RegimeBody data={query.data} />
      ) : null}
    </Page>
  );
}

function RegimeBody({ data }: { data: RegimeResponse }) {
  const reg = REGIMES[data.regime];
  const token = regimeColorToken(data.regime);
  const reasoning = data.reasoning;

  const dom = readDominance(reasoning);
  // marketcap_dom arrives as a fraction (0.594) OR a percent (59.4); normalise.
  const domPct = dom.btcDominance === null ? null : dom.btcDominance < 1.5 ? dom.btcDominance * 100 : dom.btcDominance;
  const score = asNumber(reasoning.score);
  const flowScore = asNumber(asObjectScore(reasoning.flow));
  const newsScore = asNumber(asObjectScore(reasoning.news));
  const contributions = readContributions(reasoning);

  const metrics: Array<{ label: string; value: string; sub: string; subTone?: string; tone?: string }> = [
    {
      label: 'BTC dominance',
      value: domPct === null ? '—' : `${domPct.toFixed(1)}%`,
      sub:
        dom.change24h === null
          ? 'sector spotlight'
          : `${dom.change24h >= 0 ? '↑ +' : '↓ '}${dom.change24h.toFixed(2)}% / 24h`,
      subTone: dom.change24h === null ? undefined : dom.change24h >= 0 ? 'var(--win)' : 'var(--loss)',
    },
    { label: 'Confidence', value: `${data.confidence}/10`, sub: 'regime model' },
    {
      label: 'Composite score',
      value: score === null ? '—' : signed(score),
      sub: 'flow + news',
      tone: score === null ? undefined : score > 0 ? 'var(--win)' : score < 0 ? 'var(--loss)' : undefined,
    },
    { label: 'Posture', value: formatPosture(data.signal_posture), sub: 'signal sizing', tone: 'var(--warn)' },
  ];

  const reasoningRows: Array<{ k: string; v: string }> = [
    { k: 'Flow', v: flowScore === null ? '—' : `score ${signed(flowScore)}` },
    { k: 'News', v: newsScore === null ? '—' : `score ${signed(newsScore)}` },
    { k: 'Macro', v: `${data.macro_events_nearby.length} event(s) nearby` },
    {
      k: 'Dominance',
      v:
        domPct === null
          ? '—'
          : `BTC ${domPct.toFixed(1)}%${dom.change24h === null ? '' : ` (${dom.change24h >= 0 ? '+' : ''}${dom.change24h.toFixed(2)}% 24h)`}`,
    },
  ];

  const maxAbs = Math.max(3, ...contributions.map((c) => Math.abs(c.score)));

  return (
    <>
      {/* gauge hero */}
      <Card accent className="mb-6">
        <div className="grid grid-cols-1 md:grid-cols-[auto_1fr] gap-8 items-center">
          <RegimeIndicator state={data.regime} confidence={data.confidence} size={170} />
          <div>
            <div
              className="inline-flex items-center gap-2 px-[11px] py-1 rounded-sm mb-3"
              style={{ background: colorMix(token, 12), border: `1px solid ${colorMix(token, 30)}`, color: cssVar(token) }}
            >
              <span className="font-mono text-[11px] uppercase tracking-[0.1em] font-semibold">{reg.label}</span>
            </div>
            <h2 className="text-[24px] font-semibold tracking-[-0.02em] mb-2 max-w-[560px] leading-[1.2]">
              {formatRegime(data.regime)} phase — posture {formatPosture(data.signal_posture).toLowerCase()}.
            </h2>
            <p className="text-t2 text-[14px] leading-[1.6] max-w-[600px]">
              Signal sizing follows the current regime read. Posture is{' '}
              <b className="text-warn">{formatPosture(data.signal_posture).toLowerCase()}</b>, set from a composite
              of 7-day ETF flow trend, 24h news velocity, and nearby macro events.
            </p>
          </div>
        </div>
      </Card>

      {/* metric band */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-line-2 border border-line-2 rounded-lg overflow-hidden mb-6">
        {metrics.map((m) => (
          <div key={m.label} className="bg-bg-2 p-5">
            <div className="font-mono text-[9.5px] text-t3 tracking-[0.1em] uppercase mb-2.5">{m.label}</div>
            <div className="tabular-nums text-[24px] font-semibold capitalize" style={{ letterSpacing: '-0.02em', color: m.tone ?? 'var(--t1)' }}>
              {m.value}
            </div>
            <div className="font-mono text-[10px] mt-[7px]" style={{ color: m.subTone ?? 'var(--t4)' }}>
              {m.sub}
            </div>
          </div>
        ))}
      </div>

      {/* component reasoning + score contributions */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
        <Card>
          <SectionHeader kicker="Why this regime" title="Component reasoning" />
          {reasoningRows.map((r, i) => (
            <div
              key={r.k}
              className="grid gap-3 py-3 items-baseline"
              style={{ gridTemplateColumns: '90px 1fr', borderTop: i ? '1px solid var(--line-1)' : 'none' }}
            >
              <span className="font-mono text-[11px] text-acc-hi capitalize tracking-[0.04em]">{r.k}</span>
              <span className="text-[13px] text-t2 leading-[1.5]">{r.v}</span>
            </div>
          ))}
        </Card>

        <Card>
          <SectionHeader kicker="Directional inputs" title="What's driving the score" sub="Signed contribution to the composite regime score" />
          {contributions.length === 0 ? (
            <p className="text-t3 text-[13px]">No scored contributions recorded for this snapshot.</p>
          ) : (
            contributions.map((c) => {
              const pos = c.score >= 0;
              const width = (Math.abs(c.score) / maxAbs) * 50;
              return (
                <div key={c.label} className="grid items-center gap-3 mb-3" style={{ gridTemplateColumns: '60px 1fr 44px' }}>
                  <span className="text-[13px]">{c.label}</span>
                  <div className="relative h-2 bg-bg-3 rounded-full">
                    <div className="absolute top-0 bottom-0 w-px bg-line-3" style={{ left: '50%' }} />
                    <div
                      className="absolute top-0 bottom-0 rounded-full"
                      style={{
                        background: cssVar(pos ? '--win' : '--loss'),
                        left: pos ? '50%' : `${50 - width}%`,
                        width: `${width}%`,
                      }}
                    />
                  </div>
                  <span className="font-mono tabular-nums text-[12px] text-right font-semibold" style={{ color: cssVar(pos ? '--win' : '--loss') }}>
                    {signed(c.score)}
                  </span>
                </div>
              );
            })
          )}
        </Card>
      </div>

      {/* history */}
      <RegimeHistoryStrip />
    </>
  );
}

/** flow/news sub-objects carry a `score` field; pull it for the metric rows. */
function asObjectScore(v: unknown): unknown {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>).score : null;
}

function RegimeLoading() {
  return (
    <div className="flex flex-col gap-4">
      <Skeleton className="h-40 w-full" />
      <Skeleton className="h-20 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}
