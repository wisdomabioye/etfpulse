/**
 * Orders panel — filterable table of the user's orders + per-row Cancel flow
 * (prepare-cancel → maybe sign → submit-cancel; PENDING orders cancel locally
 * with no wallet prompt).
 */

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useSignTypedData } from 'wagmi';

import type { OrderOut } from '../../api/execution';
import { useOrders, usePrepareCancel, useSubmitCancel } from '../../hooks/useExecution';
import { cssVar } from '../../lib/colorMix';
import { formatPrice, trimDecimal } from '../../lib/format';
import { sideDisplay } from '../../lib/orderSide';
import { toSodexTypedSignature } from '../../lib/sodex-sig';
import { AssetBadge, Card, FilterPill, StatusDot } from '../ui';
import { ErrorBanner } from './ErrorBanner';
import { formatError } from './execErrors';
import {
  CANCELABLE_STATUSES,
  ORDER_FILTERS,
  matchesOrderFilter,
  orderStatusMeta,
  type OrderFilter,
} from './execStatus';

export function OrdersTableSection() {
  const orders = useOrders();
  const [filter, setFilter] = useState<OrderFilter>('open');
  const rows = useMemo(
    () => orders.data?.items.filter((o) => matchesOrderFilter(o.status, filter)) ?? [],
    [orders.data, filter],
  );
  return (
    <Card pad={false}>
      <div className="px-[18px] py-3.5 border-b border-line-2 flex justify-between items-center gap-2.5 flex-wrap">
        <span className="text-[15px] font-semibold">Orders</span>
        <div className="flex gap-1.5">
          {ORDER_FILTERS.map((f) => (
            <FilterPill key={f} active={filter === f} onClick={() => setFilter(f)}>
              {f}
            </FilterPill>
          ))}
        </div>
      </div>
      {orders.error && (
        <div className="p-4">
          <ErrorBanner error={orders.error} fallback="Failed to load orders." />
        </div>
      )}
      {!orders.isLoading && rows.length === 0 && (
        <p className="text-t3 text-sm px-[18px] py-5">
          No {filter === 'all' ? '' : `${filter} `}orders.
        </p>
      )}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse min-w-[640px]">
            <thead>
              <tr>
                {(
                  ['Order', 'Asset', 'Side', 'Type', 'Size', 'Price', 'Status', 'Signal', ''] as const
                ).map((c, i) => (
                  <th
                    key={c || 'action'}
                    className={`font-mono text-[9.5px] text-t4 tracking-[0.08em] uppercase font-medium px-[18px] py-2.5 ${
                      i > 3 && i < 6 ? 'text-right' : 'text-left'
                    }`}
                  >
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((o) => (
                <OrderRow key={o.id} order={o} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function OrderRow({ order }: { order: OrderOut }) {
  const prepareCancel = usePrepareCancel();
  const submitCancel = useSubmitCancel();
  const { signTypedDataAsync } = useSignTypedData();
  const [error, setError] = useState<string | null>(null);
  // Spans the whole prepare→(sign)→submit ceremony so the button reflects
  // the wallet-signing step too (the mutation `isPending` flags go quiet
  // while MetaMask is open). Also tracks whether we're past the sign so
  // the label can switch Cancelling… → Signing… appropriately.
  const [phase, setPhase] = useState<null | 'cancelling' | 'signing'>(null);

  async function onCancel() {
    setError(null);
    setPhase('cancelling');
    try {
      const prepared = await prepareCancel.mutateAsync(order.id);
      if (prepared.local_only || prepared.replayed) {
        // PENDING or terminal — backend handled it without signing.
        return;
      }
      if (!prepared.typed_data) {
        setError('Backend did not return cancel typed-data.');
        return;
      }
      setPhase('signing');
      const signature = await signTypedDataAsync(
        prepared.typed_data as unknown as Parameters<typeof signTypedDataAsync>[0],
      );
      const wireSig = toSodexTypedSignature(signature);
      await submitCancel.mutateAsync({ orderId: order.id, signature: wireSig });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setPhase(null);
    }
  }

  const busy = phase !== null;
  const canCancel = CANCELABLE_STATUSES.has(order.status);
  const st = orderStatusMeta(order.status);
  const sd = sideDisplay(order.venue, order.side);

  return (
    <tr className="border-t border-line-1">
      <td className="px-[18px] py-3 font-mono text-[11px] text-t3">{order.id}</td>
      <td className="px-[18px] py-3">
        <AssetBadge asset={order.asset} size="sm" />
      </td>
      <td className="px-[18px] py-3">
        <span className="font-mono text-[11px]" style={{ color: cssVar(sd.token) }}>
          {sd.glyph ? `${sd.glyph} ` : ''}
          {sd.label}
        </span>
      </td>
      <td className="px-[18px] py-3 font-mono text-[11px] text-t3">
        {order.order_type.replace('_', ' ')}
      </td>
      <td className="px-[18px] py-3 text-right font-mono tabular-nums text-[11px]">
        {trimDecimal(order.requested_size)}
      </td>
      <td className="px-[18px] py-3 text-right font-mono tabular-nums text-[11px]">
        {order.requested_price == null ? '—' : formatPrice(Number(order.requested_price))}
      </td>
      <td className="px-[18px] py-3">
        <span className="inline-flex items-center gap-1.5">
          <StatusDot color={st.color} className={st.pulse ? 'animate-pulse' : ''} />
          <span className="font-mono text-[11px] text-t2">{st.label}</span>
        </span>
      </td>
      <td className="px-[18px] py-3">
        {order.signal_id != null ? (
          <Link to={`/signals/${order.signal_id}`} className="font-mono text-[11px] text-acc-hi">
            #{order.signal_id}
          </Link>
        ) : (
          <span className="font-mono text-[11px] text-t4">—</span>
        )}
      </td>
      <td className="px-[18px] py-3 text-right">
        {canCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="text-[12px] px-[11px] py-[6px] rounded-sm text-t2 border border-line-2 hover:text-t1 hover:border-line-3 disabled:opacity-50"
          >
            {phase === 'signing' ? 'Signing…' : phase === 'cancelling' ? 'Cancelling…' : 'Cancel'}
          </button>
        )}
        {error && <div className="text-[11px] text-loss mt-1 text-right">{error}</div>}
      </td>
    </tr>
  );
}
