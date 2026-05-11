import { formatUsdPrice } from '../../lib/format';

interface SpotAtSignalProps {
  price: number | null;
  source: string | null;
}

/**
 * The live spot price at signal-build time, with provenance — independent
 * of AI analysis state. Rendered above the AI/empty-state block on the
 * detail page so traders always have a market anchor even when AI
 * enrichment failed (insufficient credits, API timeout, schema mismatch).
 *
 * Renders nothing when `price === null` (both upstream providers failed
 * at build time — backfill script will revisit). Caller doesn't need a
 * conditional; the component owns its own null case so wiring stays
 * one line.
 */
export function SpotAtSignal({ price, source }: SpotAtSignalProps) {
  if (price === null) return null;
  return (
    <div className="mb-8 inline-flex items-baseline gap-2 px-3 py-1.5 rounded-md bg-bg-2 border border-border-2 font-mono text-[13px] tabular-nums">
      <span className="text-text-3 uppercase tracking-[0.1em] text-[10px]">Spot at signal</span>
      <span className="text-text-1 font-semibold">{formatUsdPrice(price)}</span>
      {source && <span className="text-text-3 text-[10px]">via {source}</span>}
    </div>
  );
}
