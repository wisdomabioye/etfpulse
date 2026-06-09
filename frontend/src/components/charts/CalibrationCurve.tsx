import { useState } from 'react';

import type { TimeHorizon } from '../../api/types';
import { BUCKET_IDEAL, CONF_BUCKETS } from '../../lib/constants';
import { formatPct } from '../../lib/format';
import type { CalibrationCell } from './types';

interface CalibrationCurveProps {
  /** One cell per confidence bucket (length must match `CONF_BUCKETS`). */
  cells: CalibrationCell[];
  horizon: TimeHorizon;
  height?: number;
  showCI?: boolean;
}

interface HoverState {
  cell: CalibrationCell;
  i: number;
  color: string;
}

/**
 * Calibration reliability curve — the signature visualization. Plots observed
 * hit-rate per confidence bucket against the ideal diagonal, with Wilson 95%
 * CI whiskers and points colored by miscalibration direction (above the
 * diagonal = underclaimed → win, below = overclaimed → loss). Ported 1:1 from
 * the prototype with the exact plot geometry; copy is accent-agnostic.
 */
export function CalibrationCurve({
  cells,
  horizon,
  height = 320,
  showCI = true,
}: CalibrationCurveProps) {
  const buckets = CONF_BUCKETS;
  const ideal = BUCKET_IDEAL;
  const W = 560;
  const H = height;
  const padL = 46;
  const padB = 38;
  const padT = 18;
  const padR = 18;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const x = (i: number) => padL + (i / (buckets.length - 1)) * plotW;
  const y = (v: number) => padT + (1 - v) * plotH;
  const [hover, setHover] = useState<HoverState | null>(null);

  // Observed polyline (skip insufficient cells).
  const obs = cells
    .map((c, i) => (c.insufficient ? null : ([x(i), y(c.hit)] as const)))
    .filter((p): p is readonly [number, number] => p !== null);
  const obsPath = obs.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');

  return (
    <div style={{ position: 'relative' }}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        style={{ display: 'block', overflow: 'visible' }}
        role="img"
        aria-label={`Calibration curve for ${horizon} horizon: observed hit-rate per confidence bucket against the ideal diagonal`}
      >
        {/* gridlines */}
        {[0, 0.25, 0.5, 0.75, 1].map((g) => (
          <g key={g}>
            <line x1={padL} y1={y(g)} x2={W - padR} y2={y(g)} stroke="var(--line-1)" strokeWidth="1" />
            <text
              x={padL - 8}
              y={y(g) + 3}
              textAnchor="end"
              fontSize="9"
              fontFamily="var(--mono)"
              fill="var(--t4)"
            >
              {(g * 100) | 0}%
            </text>
          </g>
        ))}

        {/* ideal diagonal */}
        <line
          x1={x(0)}
          y1={y(ideal[0])}
          x2={x(buckets.length - 1)}
          y2={y(ideal[ideal.length - 1])}
          stroke="var(--t4)"
          strokeWidth="1.3"
          strokeDasharray="4 4"
        />
        <text
          x={W - padR}
          y={y(ideal[ideal.length - 1]) - 6}
          textAnchor="end"
          fontSize="9"
          fontFamily="var(--mono)"
          fill="var(--t4)"
        >
          ideal
        </text>

        {/* CI whiskers */}
        {showCI &&
          cells.map((c, i) =>
            c.insufficient ? null : (
              <g key={`ci${i}`}>
                <line x1={x(i)} y1={y(c.ci_low)} x2={x(i)} y2={y(c.ci_high)} stroke="var(--line-3)" strokeWidth="1.5" />
                <line x1={x(i) - 4} y1={y(c.ci_low)} x2={x(i) + 4} y2={y(c.ci_low)} stroke="var(--line-3)" strokeWidth="1.5" />
                <line x1={x(i) - 4} y1={y(c.ci_high)} x2={x(i) + 4} y2={y(c.ci_high)} stroke="var(--line-3)" strokeWidth="1.5" />
              </g>
            ),
          )}

        {/* observed line */}
        <path
          d={obsPath}
          fill="none"
          stroke="var(--acc)"
          strokeWidth="2.2"
          strokeLinejoin="round"
          strokeLinecap="round"
          style={{ filter: 'drop-shadow(0 0 6px color-mix(in oklab, var(--acc) 50%, transparent))' }}
        />

        {/* points — colored by miscalibration direction */}
        {cells.map((c, i) => {
          if (c.insufficient) {
            return (
              <text
                key={`pt${i}`}
                x={x(i)}
                y={H - padB + 22}
                textAnchor="middle"
                fontSize="9"
                fontFamily="var(--mono)"
                fill="var(--t4)"
              >
                —
              </text>
            );
          }
          const above = c.hit >= ideal[i];
          const color = above ? 'var(--win)' : 'var(--loss)';
          return (
            <g
              key={`pt${i}`}
              onMouseEnter={() => setHover({ cell: c, i, color })}
              onMouseLeave={() => setHover(null)}
              style={{ cursor: 'pointer' }}
            >
              <circle cx={x(i)} cy={y(c.hit)} r="9" fill="transparent" />
              <circle
                cx={x(i)}
                cy={y(c.hit)}
                r={hover && hover.i === i ? 5.5 : 4.2}
                fill={color}
                stroke="var(--bg-1)"
                strokeWidth="1.5"
              />
            </g>
          );
        })}

        {/* x labels */}
        {buckets.map((b, i) => (
          <text
            key={`x${i}`}
            x={x(i)}
            y={H - padB + 14}
            textAnchor="middle"
            fontSize="9.5"
            fontFamily="var(--mono)"
            fill="var(--t3)"
          >
            {b}
          </text>
        ))}
        <text
          x={padL + plotW / 2}
          y={H - 2}
          textAnchor="middle"
          fontSize="9"
          fontFamily="var(--mono)"
          fill="var(--t4)"
          letterSpacing="0.1em"
        >
          CONFIDENCE BUCKET →
        </text>
      </svg>

      {hover && (
        <div
          style={{
            position: 'absolute',
            left: `${(x(hover.i) / W) * 100}%`,
            top: -4,
            transform: 'translate(-50%,-100%)',
            pointerEvents: 'none',
            background: 'var(--bg-4)',
            border: '1px solid var(--line-3)',
            borderRadius: 'var(--r-md)',
            padding: '9px 11px',
            boxShadow: 'var(--shadow-2)',
            whiteSpace: 'nowrap',
            zIndex: 5,
          }}
        >
          <div className="font-mono" style={{ fontSize: 10, color: 'var(--t3)', marginBottom: 4 }}>
            conf {buckets[hover.i]} · {horizon}
          </div>
          <div className="font-mono tabular-nums" style={{ fontSize: 15, fontWeight: 600, color: hover.color }}>
            {formatPct(hover.cell.hit)}
            <span style={{ color: 'var(--t3)', fontWeight: 400, fontSize: 11 }}> hit</span>
          </div>
          <div className="font-mono tabular-nums" style={{ fontSize: 10, color: 'var(--t3)', marginTop: 3 }}>
            95% CI {formatPct(hover.cell.ci_low, 0)}–{formatPct(hover.cell.ci_high, 0)}
          </div>
          <div className="font-mono tabular-nums" style={{ fontSize: 10, color: 'var(--t4)' }}>
            n={hover.cell.n} · {hover.cell.wins}W
          </div>
        </div>
      )}
    </div>
  );
}
