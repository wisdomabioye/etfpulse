/**
 * Execute page — place + manage SoDEX orders.
 *
 * Sections (all conditional on user state):
 *
 *   1. AccountStrip      — wallet, paper-trade badge, account_id status
 *   2. ApiKeyForm        — shown when at least one venue lacks an api_key
 *   3. OrderForm         — new order ceremony (prepare → sign → submit)
 *   4. OrdersTable       — open orders, polled, with Cancel
 *   5. PositionsList     — open positions, polled
 *
 * Auth-gating: `<RequireAuth>` redirects to /login if isAuthed=false.
 * The Execute route in App.tsx wraps the page in this guard so a
 * direct URL hit doesn't render an empty unauthed shell.
 *
 * Signing flow (new order):
 *   1. User fills the form, clicks Place Order.
 *   2. `usePrepareNew()` POSTs `/api/execution/prepare` → backend
 *      returns `{ order_id, client_order_id, nonce, typed_data }`.
 *   3. wagmi `useSignTypedData()` opens the wallet's signing prompt
 *      with the backend-supplied EIP-712 envelope.
 *   4. Wallet returns a 65-byte ECDSA signature.
 *   5. `toSodexTypedSignature()` normalises `v` and prepends `0x01`.
 *   6. `useSubmitNew()` POSTs the normalised signature; backend
 *      forwards to the SoDEX gateway (or paper-fills).
 *
 * Cancel flow mirrors this with `prepare-cancel` + `submit-cancel`,
 * plus a short-circuit branch for PENDING orders that the backend
 * resolves locally (no wallet prompt needed; `local_only=true`).
 */

import { useMemo, useState, type FormEvent } from 'react';
import { Navigate } from 'react-router-dom';
import { useSignTypedData } from 'wagmi';

import { ApiError } from '../api/client';
import type {
  OrderOut,
  PositionOut,
  PrepareNewRequest,
  Venue,
  WalletMeResponse,
} from '../api/execution';
import { useAuth } from '../auth/useAuth';
import {
  useOrders,
  usePositions,
  usePrepareCancel,
  usePrepareNew,
  useSetApiKey,
  useSubmitCancel,
  useSubmitNew,
  useSymbols,
  useWalletMe,
} from '../hooks/useExecution';
import { toSodexTypedSignature } from '../lib/sodex-sig';

// Order statuses that can be cancelled. Anything else either has no
// venue presence yet (PENDING is a local cancel) or is already
// terminal (FILLED, CANCELLED, REJECTED, EXPIRED).
const CANCELABLE_STATUSES = new Set([
  'pending',
  'acked',
  'partially_filled',
]);

const NON_TERMINAL_STATUSES = new Set([
  'pending',
  'submitted',
  'acked',
  'partially_filled',
]);

export function Execute() {
  const { isAuthed } = useAuth();
  if (!isAuthed) {
    return <Navigate to="/login" replace />;
  }
  return <ExecuteInner />;
}

function ExecuteInner() {
  const me = useWalletMe();

  if (me.isLoading) return <PageShell><p className="text-text-2">Loading…</p></PageShell>;
  if (me.error) {
    return (
      <PageShell>
        <ErrorBanner error={me.error} fallback="Failed to load account state." />
      </PageShell>
    );
  }
  if (!me.data) return null;

  return (
    <PageShell>
      <AccountStrip me={me.data} />
      <ApiKeySection me={me.data} />
      <OrderFormSection me={me.data} />
      <OrdersTableSection />
      <PositionsSection />
    </PageShell>
  );
}

function PageShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="max-w-5xl mx-auto px-6 py-8 space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">Execute</h1>
        <p className="text-text-2 text-sm">Trade BTC + ETH on SoDEX from your bound wallet.</p>
      </header>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 1. AccountStrip
// ---------------------------------------------------------------------------

function AccountStrip({ me }: { me: WalletMeResponse }) {
  return (
    <section className="rounded-xl border border-border-2 p-4 flex flex-wrap items-center gap-4 text-sm">
      <div>
        <div className="text-text-2 text-xs uppercase tracking-wide">Wallet</div>
        <code className="text-text-1">{me.wallet_address ?? '—'}</code>
      </div>
      <div>
        <div className="text-text-2 text-xs uppercase tracking-wide">SoDEX account</div>
        <code className="text-text-1">{me.sodex_account_id ?? '—'}</code>
      </div>
      <div className="ml-auto">
        {me.paper_trade ? (
          <span className="px-3 py-1 rounded-full bg-blue-500/20 text-blue-300 text-xs font-medium">
            PAPER TRADE
          </span>
        ) : (
          <span className="px-3 py-1 rounded-full bg-red-500/20 text-red-300 text-xs font-medium">
            REAL FUNDS
          </span>
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// 2. ApiKeyForm — visible when at least one venue is missing the named key
// ---------------------------------------------------------------------------

function ApiKeySection({ me }: { me: WalletMeResponse }) {
  const missing: Venue[] = [];
  if (!me.sodex_spot_api_key_name) missing.push('sodex_spot');
  if (!me.sodex_perps_api_key_name) missing.push('sodex_perps');
  if (missing.length === 0) return null;
  return <ApiKeyForm missing={missing} initialAccountId={me.sodex_account_id} />;
}

function ApiKeyForm({
  missing,
  initialAccountId,
}: {
  missing: Venue[];
  initialAccountId: number | null;
}) {
  const setKey = useSetApiKey();
  const [venue, setVenue] = useState<Venue>(missing[0]);
  const [apiKeyName, setApiKeyName] = useState('');
  const [accountId, setAccountId] = useState<string>(initialAccountId ? String(initialAccountId) : '');

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const parsedAccountId = Number(accountId);
    if (!Number.isFinite(parsedAccountId) || parsedAccountId <= 0) return;
    if (!apiKeyName) return;
    await setKey.mutateAsync({
      venue,
      api_key_name: apiKeyName,
      sodex_account_id: parsedAccountId,
    });
    setApiKeyName('');
  }

  return (
    <section className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 space-y-3">
      <h2 className="text-lg font-semibold text-amber-200">Bind SoDEX API key</h2>
      <p className="text-text-2 text-sm">
        Register a named API key on the SoDEX frontend, then paste its <strong>name</strong>{' '}
        (NOT the EVM address) here. ETFPulse never sees or stores your private key — the
        gateway looks up your named key under the account id below.
      </p>
      <form onSubmit={onSubmit} className="grid grid-cols-1 sm:grid-cols-4 gap-3">
        <select
          value={venue}
          onChange={(e) => setVenue(e.target.value as Venue)}
          className="px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
        >
          {missing.map((v) => (
            <option key={v} value={v}>
              {v === 'sodex_spot' ? 'Spot' : 'Perps'}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="api_key_name"
          value={apiKeyName}
          onChange={(e) => setApiKeyName(e.target.value)}
          pattern="^[A-Za-z0-9_\-]+$"
          required
          className="px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
        />
        <input
          type="number"
          placeholder="sodex_account_id"
          value={accountId}
          onChange={(e) => setAccountId(e.target.value)}
          min={1}
          required
          className="px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
        />
        <button
          type="submit"
          disabled={setKey.isPending}
          className="px-4 py-2 rounded-lg bg-accent-1 text-bg-0 font-medium disabled:opacity-50"
        >
          {setKey.isPending ? 'Saving…' : 'Bind key'}
        </button>
      </form>
      {setKey.error && (
        <ErrorBanner error={setKey.error} fallback="Failed to bind API key." />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// 3. OrderForm
// ---------------------------------------------------------------------------

function OrderFormSection({ me }: { me: WalletMeResponse }) {
  // Hide the form until at least one venue is bound + an account id exists;
  // without these the backend would 403 on prepare with `api_key_not_registered`
  // or `sodex_account_not_cached`.
  const hasAnyKey = !!(me.sodex_spot_api_key_name || me.sodex_perps_api_key_name);
  if (!hasAnyKey || !me.sodex_account_id) return null;
  return <OrderForm me={me} />;
}

function OrderForm({ me }: { me: WalletMeResponse }) {
  const [venue, setVenue] = useState<Venue>(
    me.sodex_spot_api_key_name ? 'sodex_spot' : 'sodex_perps',
  );
  const symbols = useSymbols(venue);
  const prepare = usePrepareNew();
  const submit = useSubmitNew();
  const { signTypedDataAsync } = useSignTypedData();

  const [asset, setAsset] = useState('BTC');
  const [side, setSide] = useState<'buy' | 'sell'>('buy');
  const [orderType, setOrderType] = useState<'limit' | 'market'>('limit');
  const [tif, setTif] = useState<'gtc' | 'ioc' | 'gtx'>('gtc');
  const [size, setSize] = useState('');
  const [price, setPrice] = useState('');
  const [leverage, setLeverage] = useState('');
  const [flowError, setFlowError] = useState<string | null>(null);
  const [flowSuccess, setFlowSuccess] = useState<string | null>(null);

  // Pre-filter the asset dropdown to assets that have a symbol on this
  // venue. Empty list → backend would 503 with SymbolNotResolved; render
  // an explanatory inline state instead of letting the user submit.
  const assetOptions = useMemo(() => {
    if (!symbols.data) return [];
    const venueSymbols = symbols.data.items.filter((s) => s.venue === venue);
    return Array.from(new Set(venueSymbols.map((s) => s.asset))).sort();
  }, [symbols.data, venue]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setFlowError(null);
    setFlowSuccess(null);
    if (!size || (orderType === 'limit' && !price)) {
      setFlowError('Size and (for limit orders) price are required.');
      return;
    }
    const req: PrepareNewRequest = {
      venue,
      asset,
      side,
      order_type: orderType,
      time_in_force: tif,
      requested_size: size,
      requested_price: orderType === 'limit' ? price : null,
      position_side: venue === 'sodex_perps' ? 'both' : null,
      leverage: venue === 'sodex_perps' && leverage ? leverage : null,
    };
    try {
      const prepared = await prepare.mutateAsync(req);
      // Sign the EIP-712 envelope the backend constructed. viem's
      // signTypedData verifies the domain types match the typed-data
      // shape — backend MUST emit a complete `types.EIP712Domain`.
      const signature = await signTypedDataAsync(
        prepared.typed_data as unknown as Parameters<typeof signTypedDataAsync>[0],
      );
      const wireSig = toSodexTypedSignature(signature);
      const result = await submit.mutateAsync({
        orderId: prepared.order_id,
        signature: wireSig,
      });
      setFlowSuccess(
        `Order ${result.order_id} → ${result.status}${
          result.exchange_order_id ? ` (exchange ${result.exchange_order_id})` : ''
        }`,
      );
      setSize('');
      setPrice('');
    } catch (e) {
      setFlowError(formatError(e));
    }
  }

  const submitting = prepare.isPending || submit.isPending;

  return (
    <section className="rounded-xl border border-border-2 p-4 space-y-4">
      <h2 className="text-lg font-semibold">New order</h2>
      <form onSubmit={onSubmit} className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <label className="space-y-1">
          <span className="text-xs text-text-2 uppercase tracking-wide">Venue</span>
          <select
            value={venue}
            onChange={(e) => setVenue(e.target.value as Venue)}
            className="w-full px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
          >
            {me.sodex_spot_api_key_name && <option value="sodex_spot">Spot</option>}
            {me.sodex_perps_api_key_name && <option value="sodex_perps">Perps</option>}
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-xs text-text-2 uppercase tracking-wide">Asset</span>
          <select
            value={asset}
            onChange={(e) => setAsset(e.target.value)}
            disabled={assetOptions.length === 0}
            className="w-full px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
          >
            {assetOptions.length === 0 && <option>— no symbols cached —</option>}
            {assetOptions.map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-xs text-text-2 uppercase tracking-wide">Side</span>
          <select
            value={side}
            onChange={(e) => setSide(e.target.value as 'buy' | 'sell')}
            className="w-full px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
          >
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-xs text-text-2 uppercase tracking-wide">Type</span>
          <select
            value={orderType}
            onChange={(e) => setOrderType(e.target.value as 'limit' | 'market')}
            className="w-full px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
          >
            <option value="limit">Limit</option>
            <option value="market">Market</option>
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-xs text-text-2 uppercase tracking-wide">TIF</span>
          <select
            value={tif}
            onChange={(e) => setTif(e.target.value as 'gtc' | 'ioc' | 'gtx')}
            className="w-full px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
          >
            <option value="gtc">GTC</option>
            <option value="ioc">IOC</option>
            <option value="gtx">GTX (post-only)</option>
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-xs text-text-2 uppercase tracking-wide">Size</span>
          <input
            type="text"
            inputMode="decimal"
            value={size}
            onChange={(e) => setSize(e.target.value)}
            placeholder="0.01"
            required
            className="w-full px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
          />
        </label>
        {orderType === 'limit' && (
          <label className="space-y-1">
            <span className="text-xs text-text-2 uppercase tracking-wide">Price</span>
            <input
              type="text"
              inputMode="decimal"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              placeholder="65000"
              required
              className="w-full px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
            />
          </label>
        )}
        {venue === 'sodex_perps' && (
          <label className="space-y-1">
            <span className="text-xs text-text-2 uppercase tracking-wide">Leverage</span>
            <input
              type="text"
              inputMode="decimal"
              value={leverage}
              onChange={(e) => setLeverage(e.target.value)}
              placeholder="3"
              className="w-full px-3 py-2 rounded-lg bg-bg-0 border border-border-2"
            />
          </label>
        )}
        <button
          type="submit"
          disabled={submitting || assetOptions.length === 0}
          className="col-span-2 sm:col-span-4 px-4 py-3 rounded-lg bg-accent-1 text-bg-0 font-medium disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? 'Working…' : 'Place order'}
        </button>
      </form>
      {flowError && <ErrorBanner error={flowError} fallback="Order failed." />}
      {flowSuccess && (
        <div className="p-3 rounded-lg border border-green-500/30 bg-green-500/10 text-sm text-green-300">
          {flowSuccess}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// 4. OrdersTable
// ---------------------------------------------------------------------------

function OrdersTableSection() {
  // Open orders only — terminal statuses live in the (future) history page.
  const orders = useOrders();
  const open = useMemo(
    () => orders.data?.items.filter((o) => NON_TERMINAL_STATUSES.has(o.status)) ?? [],
    [orders.data],
  );
  return (
    <section className="rounded-xl border border-border-2 p-4 space-y-3">
      <h2 className="text-lg font-semibold">Open orders</h2>
      {orders.error && <ErrorBanner error={orders.error} fallback="Failed to load orders." />}
      {open.length === 0 && !orders.isLoading && (
        <p className="text-text-2 text-sm">No open orders.</p>
      )}
      {open.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-text-2 uppercase tracking-wide">
                <th className="py-2">Venue</th>
                <th>Asset</th>
                <th>Side</th>
                <th>Size</th>
                <th>Price</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {open.map((o) => (
                <OrderRow key={o.id} order={o} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function OrderRow({ order }: { order: OrderOut }) {
  const prepareCancel = usePrepareCancel();
  const submitCancel = useSubmitCancel();
  const { signTypedDataAsync } = useSignTypedData();
  const [error, setError] = useState<string | null>(null);

  async function onCancel() {
    setError(null);
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
      const signature = await signTypedDataAsync(
        prepared.typed_data as unknown as Parameters<typeof signTypedDataAsync>[0],
      );
      const wireSig = toSodexTypedSignature(signature);
      await submitCancel.mutateAsync({ orderId: order.id, signature: wireSig });
    } catch (e) {
      setError(formatError(e));
    }
  }

  const busy = prepareCancel.isPending || submitCancel.isPending;
  const canCancel = CANCELABLE_STATUSES.has(order.status);

  return (
    <tr className="border-t border-border-2">
      <td className="py-2">{order.venue === 'sodex_spot' ? 'Spot' : 'Perps'}</td>
      <td>{order.asset}</td>
      <td>{order.side}</td>
      <td>{order.requested_size}</td>
      <td>{order.requested_price ?? '—'}</td>
      <td>
        <StatusPill status={order.status} />
      </td>
      <td className="text-right">
        {canCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="text-xs px-2 py-1 rounded border border-border-2 hover:bg-bg-0 disabled:opacity-50"
          >
            {busy ? 'Cancelling…' : 'Cancel'}
          </button>
        )}
        {error && <div className="text-xs text-red-300 mt-1">{error}</div>}
      </td>
    </tr>
  );
}

function StatusPill({ status }: { status: string }) {
  const tone: Record<string, string> = {
    pending: 'bg-yellow-500/20 text-yellow-300',
    submitted: 'bg-blue-500/20 text-blue-300',
    acked: 'bg-blue-500/20 text-blue-300',
    partially_filled: 'bg-purple-500/20 text-purple-300',
    filled: 'bg-green-500/20 text-green-300',
    cancelled: 'bg-gray-500/20 text-gray-300',
    rejected: 'bg-red-500/20 text-red-300',
    expired: 'bg-gray-500/20 text-gray-300',
  };
  const cls = tone[status] ?? 'bg-gray-500/20 text-gray-300';
  return <span className={`px-2 py-0.5 rounded text-xs ${cls}`}>{status}</span>;
}

// ---------------------------------------------------------------------------
// 5. Positions
// ---------------------------------------------------------------------------

function PositionsSection() {
  const positions = usePositions();
  return (
    <section className="rounded-xl border border-border-2 p-4 space-y-3">
      <h2 className="text-lg font-semibold">Open positions</h2>
      {positions.error && (
        <ErrorBanner error={positions.error} fallback="Failed to load positions." />
      )}
      {positions.data && positions.data.items.length === 0 && (
        <p className="text-text-2 text-sm">No open positions.</p>
      )}
      {positions.data && positions.data.items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-text-2 uppercase tracking-wide">
                <th className="py-2">Venue</th>
                <th>Asset</th>
                <th>Side</th>
                <th>Size</th>
                <th>Entry</th>
                <th>Leverage</th>
              </tr>
            </thead>
            <tbody>
              {positions.data.items.map((p) => (
                <PositionRow key={p.id} pos={p} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function PositionRow({ pos }: { pos: PositionOut }) {
  return (
    <tr className="border-t border-border-2">
      <td className="py-2">{pos.venue === 'sodex_spot' ? 'Spot' : 'Perps'}</td>
      <td>{pos.asset}</td>
      <td>{pos.side}</td>
      <td>{pos.size}</td>
      <td>{pos.entry_price}</td>
      <td>{pos.leverage ?? '—'}</td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

function ErrorBanner({ error, fallback }: { error: unknown; fallback: string }) {
  return (
    <div className="p-3 rounded-lg border border-red-500/30 bg-red-500/10 text-sm text-red-300">
      {formatError(error) || fallback}
    </div>
  );
}

function formatError(e: unknown): string {
  if (e instanceof ApiError) {
    // FastAPI structured errors (e.g. 403 risk DENY) carry a JSON
    // detail object — `ApiError.detail` is the stringified statusText
    // in that case. Surface the message + status for the operator's
    // grep convenience.
    return `[${e.status}] ${e.detail}`;
  }
  if (e instanceof Error) {
    if (/rejected|denied/i.test(e.message)) return 'Wallet signature rejected.';
    return e.message;
  }
  return String(e);
}
