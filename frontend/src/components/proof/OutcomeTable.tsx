import { useNavigate } from 'react-router-dom';

import type { TrackRecordItem } from '../../api/types';
import { DETECTORS } from '../../lib/constants';
import { formatPrice, formatSignedPct } from '../../lib/format';
import { AssetBadge, ConfidenceBadge, DetectorIcon } from '../ui';
import { horizonLabel, return24hPct, return72hPct } from './outcomeRow';

interface OutcomeTableProps {
  rows: TrackRecordItem[];
}

const COLS = ['Signal', 'Asset', 'Detector', 'Conf', 'Horizon', 'Entry→Target', '24h', '72h', 'Outcome'];

function ReturnCell({ value }: { value: number | null }) {
  if (value === null) return <span className="font-mono tabular-nums text-[11px] text-t4">—</span>;
  return (
    <span className={`font-mono tabular-nums text-[11px] ${value >= 0 ? 'text-win' : 'text-loss'}`}>
      {formatSignedPct(value, 2)}
    </span>
  );
}

/**
 * Per-outcome receipts table — ported from the prototype's `OutcomeTable`
 * (9 columns incl. separate 24h / 72h return cells), mapped to real
 * `TrackRecordItem` rows. Returns are directional (`return24hPct`/`return72hPct`,
 * MARKET rows use the composite); the sticky header stays pinned on scroll.
 * Clicking a row opens the signal.
 */
export function OutcomeTable({ rows }: OutcomeTableProps) {
  const navigate = useNavigate();
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse min-w-[760px]">
        <thead>
          <tr>
            {COLS.map((c, i) => (
              <th
                key={c}
                className={`sticky top-0 z-[1] font-mono text-[9.5px] text-t4 uppercase tracking-[0.1em] font-medium px-3.5 py-2.5 bg-bg-2 ${
                  i > 2 && i < 8 ? 'text-right' : 'text-left'
                }`}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const det = DETECTORS[r.signal_type];
            return (
              <tr
                key={r.id}
                onClick={() => navigate(`/signals/${r.signal_id}`)}
                className="cursor-pointer border-t border-line-1 hover:bg-bg-3 transition-colors"
              >
                <td className="px-3.5 py-2.5 align-middle">
                  <span className="font-mono text-acc-hi text-[12px]">#{r.signal_id}</span>
                </td>
                <td className="px-3.5 py-2.5 align-middle">
                  <AssetBadge asset={r.asset} size="sm" />
                </td>
                <td className="px-3.5 py-2.5 align-middle">
                  <span className="inline-flex items-center gap-1.5 text-[12px]">
                    <DetectorIcon type={r.signal_type} size={12} />
                    {det?.short ?? r.signal_type}
                  </span>
                </td>
                <td className="px-3.5 py-2.5 align-middle text-right">
                  <ConfidenceBadge value={r.confidence} />
                </td>
                <td className="px-3.5 py-2.5 align-middle text-right">
                  <span className="font-mono text-[11px] text-t3 capitalize">
                    {horizonLabel(r.window_hours)}
                  </span>
                </td>
                <td className="px-3.5 py-2.5 align-middle text-right">
                  <span className="font-mono tabular-nums text-[11px]">
                    {formatPrice(r.entry_price, 0)}→{formatPrice(r.target_price, 0)}
                  </span>
                </td>
                <td className="px-3.5 py-2.5 align-middle text-right">
                  <ReturnCell value={return24hPct(r)} />
                </td>
                <td className="px-3.5 py-2.5 align-middle text-right">
                  <ReturnCell value={return72hPct(r)} />
                </td>
                <td className="px-3.5 py-2.5 align-middle text-right">
                  <span
                    className={`font-mono text-[11px] font-semibold ${r.hit_target ? 'text-win' : 'text-loss'}`}
                  >
                    {r.hit_target ? '✓ target' : '✗ stop'}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
