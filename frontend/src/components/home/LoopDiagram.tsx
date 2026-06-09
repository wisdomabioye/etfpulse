const STEPS: Array<{ k: string; d: string }> = [
  { k: 'Ingest', d: 'SoSoValue ETF flows' },
  { k: 'Detect', d: '5 statistical detectors' },
  { k: 'Analyze', d: 'AI + confidence + confirmation' },
  { k: 'Deliver', d: 'Web + Telegram' },
  { k: 'Execute', d: 'SoDEX — you sign' },
  { k: 'Score', d: 'vs real prices → track record' },
];

/**
 * The disciplined loop — the six-stage spine from ingest to track-record,
 * ported 1:1 from the prototype's `LoopDiagram`. Static content; wraps
 * responsively (each cell flexes to ≥150px).
 */
export function LoopDiagram() {
  return (
    <div className="flex flex-wrap items-stretch border border-line-2 rounded-lg overflow-hidden bg-bg-2">
      {STEPS.map((s, i) => (
        <div
          key={s.k}
          className="relative p-[18px]"
          style={{
            flex: '1 1 150px',
            borderRight: i < STEPS.length - 1 ? '1px solid var(--line-1)' : 'none',
          }}
        >
          <div className="font-mono text-[10px] text-acc tracking-[0.1em] mb-2">
            {String(i + 1).padStart(2, '0')}
          </div>
          <div className="text-[15px] font-semibold mb-1">{s.k}</div>
          <div className="text-[12px] text-t3 leading-[1.45]">{s.d}</div>
          {i < STEPS.length - 1 && (
            <span
              className="absolute text-line-3 text-[13px] z-[2] bg-bg-2"
              style={{ right: -7, top: '50%', transform: 'translateY(-50%)' }}
              aria-hidden
            >
              ›
            </span>
          )}
        </div>
      ))}
    </div>
  );
}
