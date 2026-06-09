/**
 * Typed readers for the open `RegimeResponse.reasoning` JSONB. The classifier
 * stamps `{ score, flow:{score,…}, news:{score,…}, macro:{…}, dominance:{…} }`
 * (see `pipeline/regime_monitor.py`); every key is read defensively so a
 * backend-side extension never breaks the page.
 */

/** Numbers in the reasoning blob arrive as either numbers OR numeric strings
 *  (the classifier writes `str(Decimal)` for dominance fields). Accept both. */
export function asNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string') {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

export function asObject(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

export interface DominanceInfo {
  btcDominance: number | null;
  change24h: number | null;
  sectorCount: number | null;
}

export function readDominance(reasoning: Record<string, unknown>): DominanceInfo {
  const d = asObject(reasoning.dominance);
  return {
    btcDominance: d ? asNumber(d.btc_dominance) : null,
    change24h: d ? asNumber(d.btc_change_pct_24h) : null,
    sectorCount: d ? asNumber(d.sector_count) : null,
  };
}

/** Signed directional contributions to the composite regime score. Only flow
 *  and news are scored directionally today (dominance/macro abstain — see the
 *  classifier docstring), so those are the two bars the page renders. */
export interface ScoreContribution {
  label: string;
  score: number;
}

export function readContributions(reasoning: Record<string, unknown>): ScoreContribution[] {
  const out: ScoreContribution[] = [];
  const flow = asObject(reasoning.flow);
  const news = asObject(reasoning.news);
  const flowScore = flow ? asNumber(flow.score) : null;
  const newsScore = news ? asNumber(news.score) : null;
  if (flowScore !== null) out.push({ label: 'Flow', score: flowScore });
  if (newsScore !== null) out.push({ label: 'News', score: newsScore });
  return out;
}
