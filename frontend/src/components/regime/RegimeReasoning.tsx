/**
 * Structured-reasoning breakdown for `/regime`.
 *
 * The backend's `RegimeResponse.reasoning` is `Record<string, unknown>` (a
 * pass-through of `regime_snapshots.reasoning` JSONB) so we type-guard each
 * sub-field before reading. Today the classifier stamps five top-level keys:
 *
 *   { score: int, flow: {...}, news: {...}, macro: {...}, dominance: {...} }
 *
 * (See `pipeline/regime_monitor.py:classify`.) Unknown keys are tolerated —
 * the writer can extend the dict freely; this consumer only renders what it
 * recognises and silently skips anything else, so a backend-side reasoning
 * extension never breaks the UI.
 *
 * Component is intentionally read-only and stateless — no hooks, no fetch,
 * no router awareness. Pass it the `reasoning` blob from `useRegime`.
 */

interface RegimeReasoningProps {
  reasoning: Record<string, unknown>;
}

export function RegimeReasoning({ reasoning }: RegimeReasoningProps) {
  const totalScore = readNumber(reasoning.score);
  const flow = readObject(reasoning.flow);
  const news = readObject(reasoning.news);
  const macro = readObject(reasoning.macro);
  const dominance = readObject(reasoning.dominance);

  // Cold-boot safety: if reasoning is empty or missing all known keys,
  // render a single muted line rather than four empty cards. The /regime
  // page itself handles the "no snapshot at all" 503 case upstream — this
  // is the "snapshot exists but reasoning JSONB is malformed/legacy" path.
  const hasAny =
    totalScore !== null ||
    flow !== null ||
    news !== null ||
    macro !== null ||
    dominance !== null;
  if (!hasAny) {
    return (
      <div className="border border-border-2 bg-bg-2 rounded-lg px-5 py-4 font-mono text-[12px] text-text-3">
        No reasoning breakdown recorded for this snapshot.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3.5">
      {flow && (
        <ReasoningCard
          title="Flow"
          score={readNumber(flow.score)}
          totalScore={totalScore}
          rows={flowRows(flow)}
        />
      )}
      {news && (
        <ReasoningCard
          title="News"
          score={readNumber(news.score)}
          totalScore={totalScore}
          rows={newsRows(news)}
        />
      )}
      {macro && (
        <ReasoningCard
          title="Macro"
          // Macro contributes events context, not a numeric score — render
          // event-count as the headline number instead of a contribution
          // score. Avoids confusing "0" displayed where flow/news show signed.
          score={null}
          totalScore={null}
          rows={macroRows(macro)}
        />
      )}
      {dominance && (
        <ReasoningCard
          title="Dominance"
          // BTC dominance is persisted on the snapshot for transparency but
          // is intentionally NOT folded into the directional score (see
          // pipeline/regime_monitor.py docstring — rising vs falling
          // dominance is ambiguous as a bull/bear signal). Show the raw
          // dominance % as the headline; no contribution score.
          score={null}
          totalScore={null}
          headline={dominanceHeadline(dominance)}
          rows={dominanceRows(dominance)}
        />
      )}
    </div>
  );
}

interface ReasoningCardProps {
  title: string;
  /** Component's signed contribution to the overall score, or null if N/A. */
  score: number | null;
  /** Total composite score — used to render the relative weight, not its
   *  own number. Null hides the weight line. */
  totalScore: number | null;
  rows: Array<{ label: string; value: string }>;
  /** Override for the right-side header text. When set, replaces the
   *  signed-score readout. Used for the Dominance card whose headline is
   *  a raw % rather than a contribution score. Color is muted. */
  headline?: string | null;
}

function ReasoningCard({
  title,
  score,
  totalScore,
  rows,
  headline,
}: ReasoningCardProps) {
  // Color the contribution by sign: positive = pos, negative = neg, zero =
  // muted. Mirrors the regime/posture color convention so the page reads
  // consistently top-to-bottom. When `headline` overrides, color is muted
  // because the value isn't directional.
  const scoreColor =
    headline != null
      ? 'var(--color-text-1)'
      : score === null
        ? 'var(--color-text-3)'
        : score > 0
          ? 'var(--color-pos)'
          : score < 0
            ? 'var(--color-neg)'
            : 'var(--color-text-3)';
  const scoreText =
    headline != null
      ? headline
      : score === null
        ? '—'
        : score > 0
          ? `+${score}`
          : String(score);

  return (
    <div className="border border-border-2 rounded-lg bg-bg-2 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2.5 bg-bg-3 border-b border-border-2 font-mono text-[10px] uppercase tracking-[0.1em] text-text-3">
        <span>{title}</span>
        <span className="tabular-nums" style={{ color: scoreColor }}>
          {scoreText}
        </span>
      </div>
      <dl className="m-0 px-4 py-3 grid gap-x-4 gap-y-2 grid-cols-[120px_1fr] font-mono text-[12px]">
        {rows.map((row) => (
          <ReasoningRow key={row.label} label={row.label} value={row.value} />
        ))}
        {score !== null && totalScore !== null && totalScore !== 0 && (
          <ReasoningRow
            label="weight"
            value={`${Math.round((Math.abs(score) / Math.abs(totalScore)) * 100)}%`}
          />
        )}
      </dl>
    </div>
  );
}

function ReasoningRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-text-3">{label}</dt>
      <dd className="m-0 text-text-1 tabular-nums break-words">{value}</dd>
    </>
  );
}

// ---------------------------------------------------------------------------
// Per-section row builders. Each tolerates missing keys so a thinned-out
// reasoning dict (e.g. when SoSoValue macro fetch failed) still renders.
// ---------------------------------------------------------------------------

function flowRows(flow: Record<string, unknown>): Array<{ label: string; value: string }> {
  const rows: Array<{ label: string; value: string }> = [];
  const windowDays = readNumber(flow.window_days);
  if (windowDays !== null) rows.push({ label: 'window', value: `${windowDays}d` });
  const combined = readString(flow.combined_usd);
  if (combined !== null) {
    rows.push({ label: 'combined', value: formatUsd(combined) });
  }
  const byAsset = readObject(flow.by_asset_usd);
  if (byAsset) {
    for (const [asset, raw] of Object.entries(byAsset)) {
      const amount = typeof raw === 'string' ? raw : null;
      if (amount !== null) {
        rows.push({ label: asset.toLowerCase(), value: formatUsd(amount) });
      }
    }
  }
  return rows;
}

function newsRows(news: Record<string, unknown>): Array<{ label: string; value: string }> {
  const rows: Array<{ label: string; value: string }> = [];
  const windowHours = readNumber(news.window_hours);
  if (windowHours !== null) rows.push({ label: 'window', value: `${windowHours}h` });
  const velocity = readNumber(news.velocity_count);
  if (velocity !== null) {
    rows.push({ label: 'velocity', value: `${velocity} items` });
  }
  return rows;
}

function dominanceHeadline(dominance: Record<string, unknown>): string | null {
  // When the sector-spotlight fetch failed, the classifier writes
  // `{available: false, fetch_error: ...}`. Show "—" so the header reads
  // intentionally empty rather than fake "0%".
  if (dominance.available !== true) return '—';
  // Backend serialises Decimal → string ("0.5943") to dodge JS-number
  // precision loss; reformat for display only.
  const raw = readString(dominance.btc_dominance);
  if (raw === null) return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  return `${(n * 100).toFixed(1)}%`;
}

function dominanceRows(
  dominance: Record<string, unknown>,
): Array<{ label: string; value: string }> {
  const rows: Array<{ label: string; value: string }> = [];

  // Failure path: just one row carrying the fetch error so users know the
  // dominance reading is intentionally blank, not "0%". Mirrors macro's
  // `fetch_error` surfacing.
  if (dominance.available !== true) {
    const fetchError = readString(dominance.fetch_error);
    if (fetchError !== null) {
      rows.push({ label: 'fetch', value: `failed: ${fetchError}` });
    } else {
      const reason = readString(dominance.reason);
      rows.push({ label: 'state', value: reason ?? 'unavailable' });
    }
    return rows;
  }

  const dom = readString(dominance.btc_dominance);
  if (dom !== null) rows.push({ label: 'btc', value: formatPct(dom) });
  const chg = readString(dominance.btc_change_pct_24h);
  if (chg !== null) rows.push({ label: '24h Δ', value: formatSignedPct(chg) });
  const sectorCount = readNumber(dominance.sector_count);
  if (sectorCount !== null) {
    rows.push({ label: 'sectors', value: String(sectorCount) });
  }
  return rows;
}

function macroRows(macro: Record<string, unknown>): Array<{ label: string; value: string }> {
  const rows: Array<{ label: string; value: string }> = [];
  const windowDays = readNumber(macro.window_days);
  if (windowDays !== null) rows.push({ label: 'window', value: `±${windowDays}d` });
  const eventsRaw = macro.events_nearby;
  const count = Array.isArray(eventsRaw) ? eventsRaw.length : 0;
  rows.push({ label: 'events', value: count === 0 ? 'none' : String(count) });
  // Surface the SoSoValue fetch failure explicitly — when present, the
  // "no events nearby" answer is unreliable and the caller should know.
  const fetchError = readString(macro.fetch_error);
  if (fetchError !== null) {
    rows.push({ label: 'fetch', value: `failed: ${fetchError}` });
  }
  return rows;
}

// ---------------------------------------------------------------------------
// Type guards. JSONB unknown → typed reads with explicit null fallback.
// ---------------------------------------------------------------------------

function readNumber(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function readString(v: unknown): string | null {
  return typeof v === 'string' ? v : null;
}

function readObject(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

// ---------------------------------------------------------------------------
// USD formatter. Backend serialises Decimal as a string (e.g. "5000000000")
// to dodge JS-number precision loss; we reformat for display only.
// ---------------------------------------------------------------------------

function formatUsd(raw: string): string {
  const n = Number(raw);
  if (!Number.isFinite(n)) return raw;
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(n);
  if (abs >= 1_000_000_000) return `${sign}$${(abs / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

// Sector-spotlight numerics are fractions ("0.5943" = 59.43%, "0.006" = 0.60%).
// Decimal precision arrives as strings; reformat to percent for display.
function formatPct(raw: string): string {
  const n = Number(raw);
  if (!Number.isFinite(n)) return raw;
  return `${(n * 100).toFixed(2)}%`;
}

function formatSignedPct(raw: string): string {
  const n = Number(raw);
  if (!Number.isFinite(n)) return raw;
  const pct = (n * 100).toFixed(2);
  return n > 0 ? `+${pct}%` : `${pct}%`;
}
