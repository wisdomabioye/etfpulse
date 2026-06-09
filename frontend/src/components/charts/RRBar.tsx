import { formatPrice } from '../../lib/format';

interface RRBarProps {
  entry: number;
  stop: number;
  target: number;
  height?: number;
}

/**
 * Risk:Reward number-line — entry / stop / target plotted low→high with a
 * loss zone (entry→stop) and profit zone (entry→target), plus the R:R ratio.
 * Ported from the prototype (its unused `maxFav`/`maxAdv`/`isShort` locals are
 * dropped — they were declared but never rendered).
 */
export function RRBar({ entry, stop, target, height = 64 }: RRBarProps) {
  const lo = Math.min(entry, stop, target);
  const hi = Math.max(entry, stop, target);
  const pad = (hi - lo) * 0.12 || 1;
  const min = lo - pad;
  const max = hi + pad;
  const rng = max - min;
  const pos = (v: number) => ((v - min) / rng) * 100;
  const rr = Math.abs(target - entry) / Math.abs(entry - stop);

  const markers: Array<[string, number, string]> = [
    ['Stop', stop, 'var(--loss)'],
    ['Entry', entry, 'var(--t1)'],
    ['Target', target, 'var(--win)'],
  ];

  return (
    <div>
      <div className="flex justify-between mb-2.5">
        <span className="font-mono text-[10px] text-t3 tracking-[0.1em] uppercase">Risk : Reward</span>
        <span className="font-mono tabular-nums text-[12px] font-semibold text-acc-hi">
          1 : {rr.toFixed(2)}
        </span>
      </div>
      <div className="relative mb-6" style={{ height }}>
        {/* track */}
        <div className="absolute left-0 right-0 bg-line-2" style={{ top: height / 2 - 1, height: 2 }} />
        {/* loss zone entry→stop */}
        <div
          className="absolute rounded-full bg-loss-soft"
          style={{
            top: height / 2 - 4,
            height: 8,
            left: `${Math.min(pos(entry), pos(stop))}%`,
            width: `${Math.abs(pos(stop) - pos(entry))}%`,
          }}
        />
        {/* profit zone entry→target */}
        <div
          className="absolute rounded-full bg-win-soft"
          style={{
            top: height / 2 - 4,
            height: 8,
            left: `${Math.min(pos(entry), pos(target))}%`,
            width: `${Math.abs(pos(target) - pos(entry))}%`,
          }}
        />
        {markers.map(([label, v, color]) => (
          <div
            key={label}
            className="absolute top-0 flex flex-col items-center"
            style={{ left: `${pos(v)}%`, height, transform: 'translateX(-50%)' }}
          >
            <span
              className="font-mono tabular-nums text-[10px] font-semibold absolute"
              style={{ color, top: -2 }}
            >
              {formatPrice(v)}
            </span>
            <div style={{ width: 2, height: height - 8, background: color, marginTop: 14, opacity: 0.5 }} />
            <div
              className="absolute rounded-full"
              style={{ width: 11, height: 11, background: color, border: '2px solid var(--bg-2)', top: height / 2 - 5.5 }}
            />
            <span
              className="font-mono text-[9px] text-t3 absolute tracking-[0.05em] uppercase"
              style={{ bottom: -18 }}
            >
              {label}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
