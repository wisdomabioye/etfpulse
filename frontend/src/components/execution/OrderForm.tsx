/**
 * New-order ceremony — prepare → sign → submit, with perps-only SL/TP and a
 * sequential chain (`executeOrderChain`) that signs up to 3× (entry + stop +
 * take-profit). SIG2X prefill fills the form from a `?signal_id` source.
 *
 * The signing/execution LOGIC is unchanged from the original Execute page;
 * this is purely an extraction.
 */

import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useSignTypedData } from 'wagmi';

import type { PrepareNewRequest, Venue, WalletMeResponse } from '../../api/execution';
import type { SignalDetail } from '../../api/types';
import { usePrepareNew, useSubmitNew, useSymbols } from '../../hooks/useExecution';
import { executeOrderChain, type ChainProgress } from '../../lib/orderChain';
import {
  defaultVenueForSuggestedAction,
  isExecutableSignal,
  sideForSuggestedAction,
} from '../../lib/signalExecute';
import { toSodexTypedSignature } from '../../lib/sodex-sig';
import { Button, Callout, Card } from '../ui';
import { ErrorBanner } from './ErrorBanner';
import { OrderCostPreview } from './OrderCostPreview';
import { OrderEntryFields } from './OrderEntryFields';
import { OrderRiskBlock } from './OrderRiskBlock';
import { formatError } from './execErrors';

interface OrderFormProps {
  me: WalletMeResponse;
  signalId: number | undefined;
  signal: SignalDetail | undefined;
}

export function OrderFormSection({ me, signalId, signal }: OrderFormProps) {
  // Hide the form until at least one venue is bound + an account id exists;
  // without these the backend would 403 on prepare with
  // `api_key_not_registered` or `sodex_account_not_cached`.
  const hasAnyKey = !!(me.sodex_spot_api_key_name || me.sodex_perps_api_key_name);
  if (!hasAnyKey || !me.sodex_account_id) return null;
  return <OrderForm me={me} signalId={signalId} signal={signal} />;
}

function OrderForm({ me, signalId, signal }: OrderFormProps) {
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
  const [leverage, setLeverage] = useState('3');
  // PR P1.5/1.6 — perps-only SL/TP/reduce_only inputs + sequential chain
  // progress. `chainProgress=null` while idle; an active `{step,total,label}`
  // drives the "Signing X of Y…" UX.
  const [stopLoss, setStopLoss] = useState('');
  const [takeProfit, setTakeProfit] = useState('');
  const [reduceOnly, setReduceOnly] = useState(false);
  const [chainProgress, setChainProgress] = useState<ChainProgress | null>(null);
  // PR P1-fix.DBLSUB-1 — synchronous in-flight guard. The `submitting`
  // derived state + disabled button isn't enough: React re-renders the
  // disabled attribute asynchronously, so a fast double-click can fire
  // onSubmit twice before the button disables → two chains → duplicate
  // entry orders (real duplicate exposure).
  const submitInFlightRef = useRef(false);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [flowSuccess, setFlowSuccess] = useState<string | null>(null);

  // SIG2X — prefill the form fields ONCE from the supplied signal (a
  // useRef guard prevents a refetch from clobbering subsequent user
  // edits). The signal_id is forwarded to the backend on prepare so
  // Order.signal_id is set for downstream per-signal analytics.
  const prefilledRef = useRef<number | null>(null);
  // The lint rule `react-hooks/set-state-in-effect` flags the setState
  // calls below — this is the legitimate one-shot exception: sync local
  // form state from an asynchronously-loaded source EXACTLY ONCE (useRef
  // guard ensures idempotence), then let the user own the form.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    const s = signal;
    if (!s) return;
    if (prefilledRef.current === s.id) return;
    if (!isExecutableSignal(s)) return; // tradeable + actionable gate
    prefilledRef.current = s.id;
    const action = s.ai_analysis!.suggested_action;
    const nextVenue = defaultVenueForSuggestedAction(action);
    const nextSide = sideForSuggestedAction(action);
    if (nextVenue) setVenue(nextVenue);
    if (nextSide) setSide(nextSide);
    setAsset(s.asset);
    // Limit order with the AI's suggested entry, when present.
    if (s.ai_analysis!.entry_price != null) {
      setOrderType('limit');
      setPrice(String(s.ai_analysis!.entry_price));
    }
    // PR P1.5 / PR P1-fix.B3 — prefill SL / TP from the AI's levels
    // unconditionally. The fields are perps-only in the form, but the
    // *state* persists across venue switches — so a user who lands via a
    // LONG signal (default venue spot) and switches to perps to use stops
    // will see the AI's suggested levels already filled in.
    if (s.ai_analysis!.stop_price != null) setStopLoss(String(s.ai_analysis!.stop_price));
    if (s.ai_analysis!.target_price != null) setTakeProfit(String(s.ai_analysis!.target_price));
    // Size: AI doesn't suggest one; leave the field blank for the user.
  }, [signal]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // Pre-filter the asset dropdown to assets that have a symbol on this
  // venue. Empty list → backend would 503 with SymbolNotResolved.
  const assetOptions = useMemo(() => {
    if (!symbols.data) return [];
    const venueSymbols = symbols.data.items.filter((s) => s.venue === venue);
    return Array.from(new Set(venueSymbols.map((s) => s.asset))).sort();
  }, [symbols.data, venue]);

  const isPerps = venue === 'sodex_perps';
  // Venue Seg options — only the venues the user has bound a key for.
  const venueOptions = useMemo(() => {
    const opts: Array<readonly [string, Venue]> = [];
    if (me.sodex_spot_api_key_name) opts.push(['Spot', 'sodex_spot']);
    if (me.sodex_perps_api_key_name) opts.push(['Perps', 'sodex_perps']);
    return opts;
  }, [me.sodex_spot_api_key_name, me.sodex_perps_api_key_name]);

  // PR P1.5 — SL/TP only count toward the sign-count on perps (the risk
  // block is perps-only; spot rejects stops at the gate).
  const slActive = isPerps && !!stopLoss;
  const tpActive = isPerps && !!takeProfit;
  const signCount = 1 + (slActive ? 1 : 0) + (tpActive ? 1 : 0);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    // PR P1-fix.DBLSUB-1 — bail synchronously if a chain is already
    // running (defeats the double-click-before-disabled race).
    if (submitInFlightRef.current) return;
    setFlowError(null);
    setFlowSuccess(null);
    if (!size || (orderType === 'limit' && !price)) {
      setFlowError('Size and (for limit orders) price are required.');
      return;
    }
    submitInFlightRef.current = true;
    const entryReq: PrepareNewRequest = {
      venue,
      asset,
      side,
      order_type: orderType,
      time_in_force: tif,
      requested_size: size,
      requested_price: orderType === 'limit' ? price : null,
      position_side: isPerps ? 'both' : null,
      leverage: isPerps && leverage ? leverage : null,
      // SIG2X — attribute the order to the source signal when one was
      // supplied via ?signal_id. NULL = ad-hoc trade.
      signal_id: signalId ?? null,
      // PR P1.5 — reduce-only is a standalone toggle (perps only). SL/TP
      // children get their own reduce_only=true from the chain helper.
      reduce_only: isPerps && reduceOnly ? true : undefined,
    };
    // Spot rejects stop attachments at the risk gate; ignore the input
    // values when the active venue is spot.
    const slForChain = isPerps ? stopLoss : '';
    const tpForChain = isPerps ? takeProfit : '';
    try {
      // viem's `signTypedData` validates the domain shape — backend MUST
      // emit a complete `types.EIP712Domain` (D.1 guarantees).
      const results = await executeOrderChain(
        { entry: entryReq, stopLoss: slForChain, takeProfit: tpForChain },
        {
          prepare: (req) => prepare.mutateAsync(req),
          sign: async (typed) => {
            const sig = await signTypedDataAsync(
              typed as unknown as Parameters<typeof signTypedDataAsync>[0],
            );
            return toSodexTypedSignature(sig);
          },
          submit: ({ orderId, signature }) => submit.mutateAsync({ orderId, signature }),
          onStep: (p) => setChainProgress(p),
        },
      );
      const entryResult = results[0];
      const extras = results.length > 1 ? ` + ${results.length - 1} child leg(s)` : '';
      setFlowSuccess(
        `Order ${entryResult.order_id} → ${entryResult.status}${
          entryResult.exchange_order_id ? ` (exchange ${entryResult.exchange_order_id})` : ''
        }${extras}`,
      );
      setSize('');
      setPrice('');
      setStopLoss('');
      setTakeProfit('');
      setReduceOnly(false);
    } catch (e) {
      setFlowError(formatError(e));
    } finally {
      setChainProgress(null);
      submitInFlightRef.current = false;
    }
  }

  const submitting = prepare.isPending || submit.isPending || chainProgress !== null;
  const noSymbols = assetOptions.length === 0;

  return (
    <Card pad={false}>
      <div className="px-[18px] py-3.5 border-b border-line-2 flex justify-between items-center">
        <span className="text-[15px] font-semibold">New order</span>
        <span className="font-mono text-[10px] text-t4">
          {me.paper_trade ? 'paper fill sim' : 'mainnet'}
        </span>
      </div>
      <form onSubmit={onSubmit} className="p-[18px] flex flex-col gap-4">
        <OrderEntryFields
          venue={venue}
          setVenue={setVenue}
          venueOptions={venueOptions}
          side={side}
          setSide={setSide}
          asset={asset}
          setAsset={setAsset}
          assetOptions={assetOptions}
          noSymbols={noSymbols}
          orderType={orderType}
          setOrderType={setOrderType}
          tif={tif}
          setTif={setTif}
          size={size}
          setSize={setSize}
          price={price}
          setPrice={setPrice}
          leverage={leverage}
          setLeverage={setLeverage}
          isPerps={isPerps}
        />

        {/* RISK-FIRST: stop + take-profit, perps only (spot rejects stops). */}
        {isPerps && (
          <OrderRiskBlock
            stopLoss={stopLoss}
            setStopLoss={setStopLoss}
            takeProfit={takeProfit}
            setTakeProfit={setTakeProfit}
            reduceOnly={reduceOnly}
            setReduceOnly={setReduceOnly}
            price={price}
            size={size}
            leverage={leverage}
          />
        )}

        {/* P2 — cost + fee + funding + cap headroom (pre-empts the cap 403). */}
        <OrderCostPreview
          asset={asset}
          isPerps={isPerps}
          orderType={orderType}
          reduceOnly={isPerps && reduceOnly}
          size={size}
          price={price}
        />

        {/* honest sign-count */}
        <Callout tone="warn">
          This places <b>{signCount}</b> order{signCount > 1 ? 's' : ''} — entry
          {slActive ? ' + stop' : ''}
          {tpActive ? ' + take-profit' : ''} — so you&apos;ll sign <b>{signCount}</b> time
          {signCount > 1 ? 's' : ''} in your wallet.
        </Callout>

        <Button
          variant="primary"
          size="lg"
          full
          as="button"
          type="submit"
          disabled={submitting || noSymbols}
        >
          {chainProgress
            ? `Signing ${chainProgress.step} of ${chainProgress.total} (${chainProgress.label.replace('_', ' ')})…`
            : submitting
              ? 'Working…'
              : `Place order · sign ${signCount}×`}
        </Button>

        {flowError && <ErrorBanner error={flowError} fallback="Order failed." />}
        {flowSuccess && <Callout tone="pos">{flowSuccess}</Callout>}
      </form>
    </Card>
  );
}
