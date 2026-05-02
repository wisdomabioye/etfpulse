/**
 * Renders `Signal.trigger_data.news_context` — the per-asset headline
 * snapshot the v2 prompt saw at signal-build time. Backend writer is
 * `pipeline/news_context.py:gather_news_context` and the persisted shape
 * is the `NewsContextItem` TypedDict over there.
 *
 * This component is intentionally tolerant of the unknown JSONB blob:
 * `trigger_data` is `Record<string, unknown>` end-to-end (the backend
 * intentionally doesn't pin a schema since each detector writes its own
 * shape). We type-guard each field per item and silently skip malformed
 * ones rather than crashing the detail page on a single bad row.
 *
 * Stage 7-P8.
 */

interface NewsContextSectionProps {
  /** The full `trigger_data` blob from `SignalDetail`. We extract
   *  `news_context` here so the caller doesn't have to repeat the guard. */
  triggerData: Record<string, unknown>;
}

interface RenderableNewsItem {
  title: string | null;
  summary: string | null;
  category: number | null;
  publishedIso: string | null;
}

/** Returns null when the signal has no news_context (most pre-Stage-7
 *  signals) — caller skips rendering the section entirely. Returns an
 *  array (possibly empty after filtering malformed items) otherwise. */
function extractNewsContext(triggerData: Record<string, unknown>): RenderableNewsItem[] | null {
  const raw = triggerData.news_context;
  if (!Array.isArray(raw)) return null;

  const items: RenderableNewsItem[] = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) continue;
    const obj = entry as Record<string, unknown>;
    items.push({
      title: typeof obj.title === 'string' ? obj.title : null,
      summary: typeof obj.summary === 'string' ? obj.summary : null,
      category: typeof obj.category === 'number' ? obj.category : null,
      publishedIso: typeof obj.published_iso === 'string' ? obj.published_iso : null,
    });
  }
  return items;
}

export function NewsContextSection({ triggerData }: NewsContextSectionProps) {
  const items = extractNewsContext(triggerData);

  // No news_context at all (legacy / pre-Stage-7 signal) — render nothing.
  // The caller (`SignalDetail`) does its own `Array.isArray(...)` guard
  // BEFORE the SectionLabel + this component to avoid an empty section
  // header on legacy signals; this null branch is the inner safety net for
  // the same case (e.g. if the caller's guard ever drifts).
  if (items === null) return null;

  if (items.length === 0) {
    return (
      <div className="border border-border-2 bg-bg-2 rounded-lg px-[18px] py-3.5 font-mono text-[13px] text-text-3">
        No news context recorded — the gatherer found no relevant items in the
        lookback window when this signal was built.
      </div>
    );
  }

  return (
    <ul className="m-0 p-0 list-none flex flex-col gap-2.5">
      {items.map((item, i) => (
        <li
          key={`${item.publishedIso ?? 'no-date'}-${i}`}
          className="border border-border-2 bg-bg-2 rounded-lg px-[18px] py-3"
        >
          <div className="flex items-baseline justify-between gap-3 flex-wrap">
            <div
              className="text-[14px] text-text-1 leading-[1.4]"
              style={{ textWrap: 'pretty' }}
            >
              {item.title ?? '(no title)'}
            </div>
            <div className="font-mono text-[10px] text-text-3 tabular-nums uppercase tracking-[0.08em] flex items-center gap-2 shrink-0">
              {item.category !== null && (
                <span className="border border-border-2 rounded px-1.5 py-0.5">
                  cat {item.category}
                </span>
              )}
              <span>{formatPublishedAt(item.publishedIso)}</span>
            </div>
          </div>
          {item.summary && (
            <div
              className="mt-1.5 text-[13px] text-text-2 leading-[1.55]"
              style={{ textWrap: 'pretty' }}
            >
              {item.summary}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function formatPublishedAt(iso: string | null): string {
  if (iso === null) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  // YYYY-MM-DD HH:mm UTC — terse, no locale juggling. Matches the
  // tabular-nums elsewhere in the page.
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`
  );
}
