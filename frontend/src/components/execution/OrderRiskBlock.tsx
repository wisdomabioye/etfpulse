/**
 * Perps-only risk-attachment block — stop-loss / take-profit inputs, the live
 * max-loss + R:R readout, and the reduce-only toggle. Spot hides this (the risk
 * gate rejects stop attachments on spot).
 */

import { colorMix, cssVar } from '../../lib/colorMix';
import { formatPrice } from '../../lib/format';
import { Field } from '../ui';

interface OrderRiskBlockProps {
  stopLoss: string;
  setStopLoss: (v: string) => void;
  takeProfit: string;
  setTakeProfit: (v: string) => void;
  reduceOnly: boolean;
  setReduceOnly: (v: boolean) => void;
  /** Entry price / size / leverage (raw form strings) — drive the live
   *  max-loss + R:R readout. Co-located here so the risk math lives with the
   *  risk UI. */
  price: string;
  size: string;
  leverage: string;
}

export function OrderRiskBlock({
  stopLoss,
  setStopLoss,
  takeProfit,
  setTakeProfit,
  reduceOnly,
  setReduceOnly,
  price,
  size,
  leverage,
}: OrderRiskBlockProps) {
  const nPrice = Number(price);
  const nStop = Number(stopLoss);
  const nSize = Number(size);
  const nLev = Number(leverage) || 1;
  const ready = !!stopLoss && nPrice > 0 && nStop > 0 && nSize > 0;
  const maxLoss = ready ? Math.abs((nPrice - nStop) * nSize) * nLev : null;
  const rr =
    ready && takeProfit && Number(takeProfit) > 0
      ? Math.abs(Number(takeProfit) - nPrice) / (Math.abs(nPrice - nStop) || 1)
      : null;
  return (
    <div className="p-3.5 bg-bg-1 border border-line-2 rounded-md">
      <div className="font-mono text-[10px] text-loss tracking-[0.1em] uppercase mb-3 flex items-center gap-[7px]">
        <span className="w-1.5 h-1.5 rounded-full bg-loss" aria-hidden />
        Risk attachment — bound the loss
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Stop-loss">
          <input
            type="text"
            inputMode="decimal"
            value={stopLoss}
            onChange={(e) => setStopLoss(e.target.value)}
            placeholder="—"
            aria-label="Stop loss price"
            className="w-full tabular-nums bg-bg-1 text-t1 rounded-sm px-2.5 py-2 text-[12.5px] font-mono border"
            style={{
              borderColor: stopLoss
                ? colorMix('--loss', 35, cssVar('--line-3'))
                : cssVar('--line-3'),
            }}
          />
        </Field>
        <Field label="Take-profit">
          <input
            type="text"
            inputMode="decimal"
            value={takeProfit}
            onChange={(e) => setTakeProfit(e.target.value)}
            placeholder="—"
            aria-label="Take profit price"
            className="w-full tabular-nums bg-bg-1 text-t1 rounded-sm px-2.5 py-2 text-[12.5px] font-mono border"
            style={{
              borderColor: takeProfit
                ? colorMix('--win', 35, cssVar('--line-3'))
                : cssVar('--line-3'),
            }}
          />
        </Field>
      </div>
      {maxLoss !== null && (
        <div className="font-mono tabular-nums text-[10.5px] text-t3 mt-2.5">
          max loss ≈ <span className="text-loss">{formatPrice(maxLoss)}</span>
          {rr !== null && (
            <>
              {' '}
              · R:R <span className="text-acc-hi">1:{rr.toFixed(2)}</span>
            </>
          )}
        </div>
      )}
      <label className="flex gap-2 items-center cursor-pointer mt-3">
        <input
          type="checkbox"
          checked={reduceOnly}
          onChange={(e) => setReduceOnly(e.target.checked)}
          aria-label="Reduce only (perps only)"
          className="accent-acc"
        />
        <span className="font-mono text-[11px] text-t2">
          reduce-only — can only close, never open new exposure
        </span>
      </label>
    </div>
  );
}
