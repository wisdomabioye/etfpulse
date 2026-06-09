/**
 * Open-positions panel + per-row Close flow (prepare → sign → submit) with a
 * styled confirm modal. Mark + uPnL come from live prices: perps marks via the
 * account-summary query, spot via `/api/prices/spot`. When no price is
 * available for a row those cells render "—" (graceful, never NaN).
 */

import { useRef, useState } from 'react';
import { useSignTypedData } from 'wagmi';

import { useSpotPrices } from '../../api/queries';
import type { PositionOut } from '../../api/execution';
import {
  useAccountSummary,
  useClosePosition,
  usePositions,
  useSubmitNew,
} from '../../hooks/useExecution';
import { cssVar } from '../../lib/colorMix';
import { formatPrice, formatSignedPct, trimDecimal } from '../../lib/format';
import { sideDisplay } from '../../lib/orderSide';
import { unrealizedPnl } from '../../lib/positionMath';
import { toSodexTypedSignature } from '../../lib/sodex-sig';
import { AssetBadge, Button, Card } from '../ui';
import { ErrorBanner } from './ErrorBanner';
import { formatError } from './execErrors';

/** Resolve a live mark for a position: perps from the account-summary mark
 *  prices (by canonical asset), spot from the BTC/ETH spot feed. null when no
 *  price is available (the row then shows "—" for Mark/uPnL). */
function useMarkResolver(): (pos: PositionOut) => number | null {
  const summary = useAccountSummary();
  const spot = useSpotPrices();
  return (pos: PositionOut) => {
    if (pos.venue === 'sodex_perps') {
      const m = summary.data?.mark_prices.find((x) => x.asset === pos.asset);
      return m ? Number(m.mark_price) : null;
    }
    const sp = spot.data;
    if (!sp) return null;
    if (pos.asset === 'BTC') return sp.btc;
    if (pos.asset === 'ETH') return sp.eth;
    return null;
  };
}

export function PositionsSection() {
  const positions = usePositions();
  const markFor = useMarkResolver();
  const items = positions.data?.items ?? [];
  return (
    <Card pad={false}>
      <div className="px-[18px] py-3.5 border-b border-line-2 flex justify-between items-center">
        <span className="text-[15px] font-semibold">Open positions</span>
        <span className="font-mono text-[11px] text-t4">{items.length} open</span>
      </div>
      {positions.error && (
        <div className="p-4">
          <ErrorBanner error={positions.error} fallback="Failed to load positions." />
        </div>
      )}
      {positions.data && items.length === 0 && (
        <p className="text-t3 text-sm px-[18px] py-5">No open positions.</p>
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse min-w-[560px]">
            <thead>
              <tr>
                {(['Asset', 'Side', 'Size', 'Entry', 'Mark', 'uPnL', 'Lev', ''] as const).map(
                  (c, i) => (
                    <th
                      key={c || 'action'}
                      className={`font-mono text-[9.5px] text-t4 tracking-[0.08em] uppercase font-medium px-[18px] py-2.5 ${
                        i > 1 && i < 7 ? 'text-right' : 'text-left'
                      }`}
                    >
                      {c}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {items.map((p) => (
                <PositionRow key={p.id} pos={p} mark={markFor(p)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function PositionRow({ pos, mark }: { pos: PositionOut; mark: number | null }) {
  const closePos = useClosePosition();
  const submit = useSubmitNew();
  const { signTypedDataAsync } = useSignTypedData();
  // Per-row in-flight guard — synchronous, defeats double-click before
  // useMutation flips its `isPending` flag.
  const inFlight = useRef(false);
  const [rowError, setRowError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  // Explicit in-flight state spanning the WHOLE prepare→sign→submit
  // ceremony. The mutation `isPending` flags have a gap during the wallet
  // signing step (prepare resolved, submit not started) — so a label
  // derived from them sits static exactly while MetaMask is open. This
  // drives the "Signing…" label for the full duration.
  const [working, setWorking] = useState(false);

  async function doClose() {
    if (inFlight.current) return;
    inFlight.current = true;
    setWorking(true);
    setRowError(null);
    try {
      const prep = await closePos.mutateAsync(pos.id);
      const sig = await signTypedDataAsync(
        prep.typed_data as unknown as Parameters<typeof signTypedDataAsync>[0],
      );
      const wireSig = toSodexTypedSignature(sig);
      await submit.mutateAsync({ orderId: prep.order_id, signature: wireSig });
      setConfirming(false);
    } catch (e) {
      setRowError(formatError(e));
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  }

  const sd = sideDisplay(pos.venue, pos.side);
  const pnl =
    mark !== null
      ? unrealizedPnl({
          side: pos.side,
          size: Number(pos.size),
          entry: Number(pos.entry_price),
          mark,
        })
      : null;
  return (
    <tr className="border-t border-line-1">
      <td className="px-[18px] py-3">
        <AssetBadge asset={pos.asset} size="sm" />
      </td>
      <td className="px-[18px] py-3">
        <span className="font-mono text-[11px]" style={{ color: cssVar(sd.token) }}>
          {sd.glyph ? `${sd.glyph} ` : ''}
          {sd.label}
        </span>
      </td>
      <td className="px-[18px] py-3 text-right font-mono tabular-nums text-[12px]">
        {trimDecimal(pos.size)}
      </td>
      <td className="px-[18px] py-3 text-right font-mono tabular-nums text-[12px]">
        {formatPrice(Number(pos.entry_price))}
      </td>
      <td className="px-[18px] py-3 text-right font-mono tabular-nums text-[12px] text-t2">
        {mark !== null ? formatPrice(mark) : '—'}
      </td>
      <td className="px-[18px] py-3 text-right font-mono tabular-nums text-[12px]">
        {pnl ? (
          <span style={{ color: cssVar(pnl.pnl >= 0 ? '--win' : '--loss') }}>
            {pnl.pnl >= 0 ? '+' : '−'}
            {formatPrice(Math.abs(pnl.pnl))}
            <span className="text-[10px]"> {formatSignedPct(pnl.pnlPct)}</span>
          </span>
        ) : (
          <span className="text-t4">—</span>
        )}
      </td>
      <td className="px-[18px] py-3 text-right font-mono tabular-nums text-[11px] text-t3">
        {pos.leverage ? `${trimDecimal(pos.leverage)}×` : '—'}
      </td>
      <td className="px-[18px] py-3 text-right">
        <button
          type="button"
          onClick={() => setConfirming(true)}
          aria-label={`Close ${pos.asset} ${pos.side} position`}
          className="px-[11px] py-[6px] text-[12px] rounded-sm bg-bg-3 text-t1 border border-line-3 font-medium hover:brightness-[1.08]"
        >
          Close
        </button>
        {rowError && (
          <div className="mt-1 text-[11px] text-loss max-w-[200px] text-right">{rowError}</div>
        )}
        {confirming && (
          <CloseConfirm
            pos={pos}
            busy={working}
            error={rowError}
            onConfirm={doClose}
            onCancel={() => {
              if (working) return;
              setConfirming(false);
            }}
          />
        )}
      </td>
    </tr>
  );
}

/** Confirm modal for closing a position (replaces the bare `window.confirm`).
 *  The actual prepare→sign→submit chain runs on confirm via `onConfirm`. */
function CloseConfirm({
  pos,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  pos: PositionOut;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const isSpot = pos.venue === 'sodex_spot';
  const sd = sideDisplay(pos.venue, pos.side);
  return (
    <div
      className="fixed inset-0 z-[300] bg-black/60 flex items-center justify-center p-6 text-left"
      onClick={onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[380px] bg-bg-2 border border-line-3 rounded-lg p-[22px]"
        style={{ boxShadow: 'var(--shadow-2)' }}
      >
        <div className="text-[16px] font-semibold mb-2">
          Close {pos.asset} {sd.label} position?
        </div>
        <div className="text-[13px] text-t3 leading-[1.55] mb-[18px]">
          {isSpot
            ? `This submits a market sell of ${trimDecimal(pos.size)} ${pos.asset} on Spot.`
            : `This submits a market reduce-only order for ${trimDecimal(pos.size)} ${pos.asset} on Perps.`}{' '}
          You&apos;ll sign <b className="text-t1">1×</b> in your wallet.
        </div>
        {error && <div className="text-[12px] text-loss mb-3">{error}</div>}
        <div className="flex gap-2.5 justify-end">
          <Button variant="ghost" size="md" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant="secondary" destructive size="md" onClick={onConfirm} disabled={busy}>
            {busy ? 'Signing…' : 'Close · sign 1×'}
          </Button>
        </div>
      </div>
    </div>
  );
}
