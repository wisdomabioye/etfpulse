/**
 * PR P1.6 — sequential order-chain submission helpers.
 *
 * V1 SL/TP support: each protective leg is a SEPARATE Order row,
 * signed independently. To open a position with attached SL + TP the
 * user signs 3 typed-data envelopes:
 *
 *   1. ENTRY  — limit/market entry (no parent)
 *   2. SL     — opposite-side stop_loss MARKET, reduce_only=true,
 *               parent_order_id=ENTRY.order_id
 *   3. TP     — opposite-side take_profit MARKET, reduce_only=true,
 *               parent_order_id=ENTRY.order_id
 *
 * If the user signs ENTRY then aborts SL, the entry order stays live
 * with NO protection — that's the documented V1 limit. A future
 * gateway-native attached-stop endpoint would collapse this to one
 * signature; for now we lean on the FE chain.
 *
 * The pure helper here is dependency-injected (prepare/sign/submit
 * fns + an onStep progress callback) so the page-level orchestration
 * is testable without React, TanStack Query, or wagmi. The same fn
 * is used live in `pages/Execute.tsx` and in `lib/orderChain.test.ts`.
 */

import type { PrepareNewRequest, PrepareNewResponse, Side, TypedData } from '../api/execution';

/** Inputs the entry's child orders need to be derived from. Pure
 *  data — no hooks, no async. */
export interface ChildLegInputs {
  /** Entry order's request payload (already validated). */
  entry: PrepareNewRequest;
  /** Optional stop-loss price. Empty / null = skip SL leg. */
  stopLoss?: string | null;
  /** Optional take-profit price. Empty / null = skip TP leg. */
  takeProfit?: string | null;
}

/** Side that closes a position opened by `entrySide`. BUY entry →
 *  SELL close, SELL entry → BUY close. */
function oppositeSide(entrySide: Side): Side {
  return entrySide === 'buy' ? 'sell' : 'buy';
}

/** Build ONE protective child leg. MARKET + IOC + reduce_only=true,
 *  plus the stop semantics the gateway needs (PR P1-fix.CRIT-1):
 *  `stop_price` + `stop_type` + `trigger_type='mark_price'` +
 *  `is_conditional=true`. Without trigger_type the BE risk gate denies
 *  (`stop_requires_trigger_type`); without all four the signed payload
 *  wouldn't arm a real stop on the venue. `parent_order_id` is filled
 *  by `executeOrderChain` after the entry's prepare resolves. */
function buildProtectiveLeg(
  entry: PrepareNewRequest,
  stopPrice: string,
  stopType: NonNullable<PrepareNewRequest['stop_type']>,
): PrepareNewRequest {
  return {
    venue: entry.venue,
    asset: entry.asset,
    side: oppositeSide(entry.side),
    order_type: 'market',
    time_in_force: 'ioc',
    requested_size: entry.requested_size,
    requested_price: null,
    position_side: entry.position_side ?? null,
    leverage: entry.leverage ?? null,
    signal_id: entry.signal_id ?? null,
    stop_price: stopPrice,
    stop_type: stopType,
    trigger_type: 'mark_price',
    is_conditional: true,
    reduce_only: true,
  };
}

/** Build the SL/TP child PrepareNewRequest payloads given the entry.
 *
 *  Returns an empty array when neither SL nor TP is requested. The
 *  caller can short-circuit the chain to a single-leg submit.
 */
export function buildChildRequests({
  entry,
  stopLoss,
  takeProfit,
}: ChildLegInputs): PrepareNewRequest[] {
  const out: PrepareNewRequest[] = [];
  if (stopLoss && stopLoss.trim() !== '') {
    out.push(buildProtectiveLeg(entry, stopLoss, 'stop_loss'));
  }
  if (takeProfit && takeProfit.trim() !== '') {
    out.push(buildProtectiveLeg(entry, takeProfit, 'take_profit'));
  }
  return out;
}

export interface ChainStepResult {
  order_id: number;
  status: string;
  exchange_order_id?: string | null;
}

export interface ChainProgress {
  step: number; // 1-indexed
  total: number;
  label: 'entry' | 'stop_loss' | 'take_profit';
}

export interface ChainDeps {
  prepare(req: PrepareNewRequest): Promise<PrepareNewResponse>;
  sign(typed: TypedData): Promise<string>; // returns the wire-format `0x01…` hex
  submit(args: { orderId: number; signature: string }): Promise<ChainStepResult>;
  onStep?(progress: ChainProgress): void;
}

/** Run the full chain: ENTRY → (SL) → (TP). Returns the array of
 *  per-leg submit results. Throws on the first failing leg (the
 *  caller decides how to surface — an orphaned entry without SL/TP
 *  is the documented V1 risk). */
export async function executeOrderChain(
  legs: ChildLegInputs,
  deps: ChainDeps,
): Promise<ChainStepResult[]> {
  const children = buildChildRequests(legs);
  const total = 1 + children.length;
  const results: ChainStepResult[] = [];

  deps.onStep?.({ step: 1, total, label: 'entry' });
  const prepEntry = await deps.prepare(legs.entry);
  const sigEntry = await deps.sign(prepEntry.typed_data);
  const subEntry = await deps.submit({ orderId: prepEntry.order_id, signature: sigEntry });
  results.push(subEntry);

  // PR P1-fix.B1/F2 — abort chain if the entry landed in any
  // terminal-failure state. Otherwise the children would be prepared
  // with parent_order_id pointing at a dead order: BE's
  // `parent_order_terminal` risk gate (D1) would 403 the child
  // prepare anyway, so this is a fast-fail to skip wasted round-trips
  // and give the user a clear "entry <status> — chain aborted"
  // message instead of a mid-chain HTTP 403. `cancelled` is included
  // for the race window where the user cancels the entry between
  // prepare and submit (submit_new's replay path returns it).
  if (
    subEntry.status === 'rejected' ||
    subEntry.status === 'expired' ||
    subEntry.status === 'cancelled'
  ) {
    throw new Error(
      `Entry order ${subEntry.order_id} ${subEntry.status} — skipping SL/TP legs`,
    );
  }

  for (let i = 0; i < children.length; i++) {
    const child = children[i];
    const childWithParent: PrepareNewRequest = {
      ...child,
      parent_order_id: prepEntry.order_id,
    };
    const label = child.stop_type === 'stop_loss' ? 'stop_loss' : 'take_profit';
    deps.onStep?.({ step: 2 + i, total, label });
    const prep = await deps.prepare(childWithParent);
    const sig = await deps.sign(prep.typed_data);
    const sub = await deps.submit({ orderId: prep.order_id, signature: sig });
    results.push(sub);
  }

  return results;
}
