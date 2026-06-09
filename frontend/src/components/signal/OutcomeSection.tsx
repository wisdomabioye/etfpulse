import type { SignalOutcome } from '../../api/types';
import type { ColorToken } from '../../lib/colorMix';
import { cssVar } from '../../lib/colorMix';
import { formatPrice, formatRemaining, formatSignedPct, hoursUntil } from '../../lib/format';

interface OutcomeSectionProps {
  outcome: SignalOutcome | null;
  /** Validity-window end — drives the dynamic "~Xh remaining" countdown. */
  expiresAt: string | null;
  /** Signal creation time — drives the window-length label. */
  createdAt: string;
  /** Spot at signal-build time (for the pending "price at signal" line). */
  priceAtCreation: number | null;
  /** 'long' | 'short' | null — signs the single-asset realised return. */
  direction: 'long' | 'short' | null;
}

function excursion(value: number | null, sign: '+' | '-'): string {
  if (value === null) return '—';
  return `${sign}${(value * 100).toFixed(1)}%`;
}

/** Realised directional return for a single-asset outcome, signed %. */
function singleReturnPct(o: SignalOutcome, direction: 'long' | 'short' | null): number | null {
  const entry = o.entry_price ?? o.price_at_signal;
  const end = o.price_at_validity_end ?? o.price_after_72h;
  if (entry === null || end === null || entry <= 0) return null;
  const raw = ((end - entry) / entry) * 100;
  return direction === 'short' ? -raw : raw;
}

/**
 * Outcome section — ported to the prototype's treatment:
 *   - **Pending** (`outcome === null`): a blinking dot + "evaluates at the
 *     {window}h validity-window close" + a LIVE "price at signal · ~Xh
 *     remaining" line, where the countdown is the true `expires_at − now`.
 *   - **Evaluated**: a 4-cell grid (Price at signal · Return · Result · Max
 *     fav/adv). MARKET rows use the composite return + "Composite hit/miss";
 *     single-asset rows use the directional realised return + Target/Stop.
 */
export function OutcomeSection({
  outcome,
  expiresAt,
  createdAt,
  priceAtCreation,
  direction,
}: OutcomeSectionProps) {
  if (outcome === null) {
    const h = expiresAt ? hoursUntil(expiresAt) : null;
    const windowH =
      expiresAt && createdAt
        ? Math.round((new Date(expiresAt).getTime() - new Date(createdAt).getTime()) / 3_600_000)
        : null;
    return (
      <div className="flex items-center gap-3.5 px-5 py-4 bg-bg-2 border border-line-2 rounded-lg">
        <span
          className="w-2.5 h-2.5 rounded-full bg-acc shrink-0"
          style={{ animation: 'blink 1.6s infinite' }}
          aria-hidden
        />
        <div>
          <div className="text-[13px] text-t2">
            Pending — evaluates at the {windowH !== null ? `${windowH}h ` : ''}validity-window close.
          </div>
          <div className="font-mono text-[11px] text-t4 mt-[3px]">
            price at signal = {formatPrice(priceAtCreation)}
            {h !== null ? ` · ${formatRemaining(h)}` : ''}
          </div>
        </div>
      </div>
    );
  }

  const isMarket = outcome.composite_return_pct !== null;
  const ret = isMarket ? outcome.composite_return_pct! * 100 : singleReturnPct(outcome, direction);

  const result = isMarket
    ? outcome.hit_target
      ? { label: '✓ Composite hit', token: '--win' as const }
      : { label: '— Composite miss', token: '--t3' as const }
    : outcome.hit_target
      ? { label: '✓ Target hit', token: '--win' as const }
      : outcome.hit_stop
        ? { label: '✗ Stopped out', token: '--loss' as const }
        : { label: '— Neither hit', token: '--t3' as const };

  const cells: Array<{ label: string; value: string; token?: ColorToken }> = [
    { label: 'Price at signal', value: formatPrice(outcome.price_at_signal ?? priceAtCreation) },
    {
      label: isMarket ? 'Composite return' : 'Return',
      value: ret === null ? '—' : formatSignedPct(ret, 2),
      token: ret === null ? undefined : ret >= 0 ? '--win' : '--loss',
    },
    { label: 'Result', value: result.label, token: result.token },
    {
      label: 'Max fav / adv',
      value: `${excursion(outcome.max_favorable, '+')} / ${excursion(outcome.max_adverse, '-')}`,
    },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3.5 px-5 py-4 bg-bg-2 border border-line-2 rounded-lg">
      {cells.map((c) => (
        <div key={c.label}>
          <div className="font-mono text-[9.5px] text-t4 tracking-[0.1em] uppercase">{c.label}</div>
          <div
            className="tabular-nums text-[16px] font-semibold mt-1"
            style={{ color: c.token ? cssVar(c.token) : 'var(--t1)' }}
          >
            {c.value}
          </div>
        </div>
      ))}
    </div>
  );
}
