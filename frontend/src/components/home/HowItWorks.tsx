/**
 * 3-cell "how it works" grid — MONITOR · ANALYZE · ALERT.
 *
 * Terminal trick per mock: the outer grid has `gap: 1px` with
 * `background: var(--border-2)`; the inner cells have `background: var(--bg-2)`.
 * The 1px gap shows through as the divider color → hairline separators
 * between cells, no double-borders.
 *
 * Copy lifted from the mock's HowItWorks component verbatim.
 */

const STEPS = [
  {
    n: '01',
    tag: 'MONITOR',
    title: 'Monitor',
    body: 'Daily inflows and outflows across every US-listed BTC and ETH ETF.',
  },
  {
    n: '02',
    tag: 'ANALYZE',
    title: 'Analyze',
    body: 'Five detectors plus AI reasoning produce a signal with confidence 1–10.',
  },
  {
    n: '03',
    tag: 'ALERT',
    title: 'Alert',
    body: 'Delivered to Telegram in minutes. Full reasoning and track record public.',
  },
];

export function HowItWorks() {
  return (
    <div
      className="grid grid-cols-1 md:grid-cols-3 gap-px rounded-lg overflow-hidden border border-border-2"
      style={{ background: 'var(--color-border-2)' }}
    >
      {STEPS.map((step) => (
        <div key={step.n} className="bg-bg-2 px-6 py-[22px]">
          <div className="font-mono text-[11px] text-accent tracking-[0.1em] mb-3.5">
            {step.n} / {step.tag}
          </div>
          <div
            className="text-[18px] font-semibold text-text-1 mb-2"
            style={{ letterSpacing: '-0.01em' }}
          >
            {step.title}
          </div>
          <div className="text-[13px] text-text-2 leading-[1.55]">{step.body}</div>
        </div>
      ))}
    </div>
  );
}
