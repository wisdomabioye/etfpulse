/**
 * PR P1.6 — sequential order-chain helper tests.
 *
 * Pure-function coverage of `buildChildRequests` + the orchestration
 * `executeOrderChain`. No React, no wagmi, no network. The chain
 * dependencies are injected so the test can pin the EXACT sequence
 * of prepare → sign → submit calls without mocking module-level
 * functions.
 */

import { describe, expect, it, vi } from 'vitest';

import type { PrepareNewRequest, PrepareNewResponse, TypedData } from '../api/execution';

import {
  buildChildRequests,
  executeOrderChain,
  type ChainProgress,
  type ChainStepResult,
} from './orderChain';

function makeEntry(over: Partial<PrepareNewRequest> = {}): PrepareNewRequest {
  return {
    venue: 'sodex_perps',
    asset: 'BTC',
    side: 'buy',
    order_type: 'limit',
    time_in_force: 'gtc',
    requested_size: '0.01',
    requested_price: '65000',
    position_side: 'both',
    leverage: '3',
    signal_id: null,
    ...over,
  };
}

function makePrepareResponse(orderId: number): PrepareNewResponse {
  return {
    order_id: orderId,
    client_order_id: `co-${orderId}`,
    nonce: 1000000 + orderId,
    typed_data: {
      types: {},
      primaryType: 'ExchangeAction',
      domain: { name: 'futures', version: '1', chainId: 138565, verifyingContract: '0x0' },
      message: {},
    } as TypedData,
  };
}

// ---------------------------------------------------------------------------
// buildChildRequests
// ---------------------------------------------------------------------------

describe('buildChildRequests', () => {
  it('returns empty array when neither SL nor TP supplied', () => {
    expect(buildChildRequests({ entry: makeEntry() })).toEqual([]);
    expect(buildChildRequests({ entry: makeEntry(), stopLoss: '', takeProfit: '' })).toEqual([]);
    expect(buildChildRequests({ entry: makeEntry(), stopLoss: null, takeProfit: null })).toEqual([]);
  });

  it('builds a SELL stop_loss child when entry is BUY + stopLoss set', () => {
    const children = buildChildRequests({ entry: makeEntry({ side: 'buy' }), stopLoss: '60000' });
    expect(children).toHaveLength(1);
    expect(children[0]).toMatchObject({
      side: 'sell',
      order_type: 'market',
      time_in_force: 'ioc',
      reduce_only: true,
      stop_price: '60000',
      stop_type: 'stop_loss',
      // PR P1-fix.CRIT-1 — the child MUST carry trigger_type +
      // is_conditional or the BE gate denies (`stop_requires_trigger_type`)
      // and the signed payload wouldn't arm a real stop.
      trigger_type: 'mark_price',
      is_conditional: true,
      requested_size: '0.01',
      requested_price: null,
    });
  });

  it('builds a BUY take_profit child when entry is SELL + takeProfit set', () => {
    const children = buildChildRequests({ entry: makeEntry({ side: 'sell' }), takeProfit: '70000' });
    expect(children).toHaveLength(1);
    expect(children[0]).toMatchObject({
      side: 'buy',
      stop_type: 'take_profit',
      stop_price: '70000',
    });
  });

  it('builds both SL and TP when both supplied', () => {
    const children = buildChildRequests({
      entry: makeEntry({ side: 'buy' }),
      stopLoss: '60000',
      takeProfit: '70000',
    });
    expect(children).toHaveLength(2);
    expect(children[0].stop_type).toBe('stop_loss');
    expect(children[1].stop_type).toBe('take_profit');
    // Both closes are opposite side (SELL when entry was BUY).
    expect(children[0].side).toBe('sell');
    expect(children[1].side).toBe('sell');
  });

  it('forwards signal_id onto children for cohort attribution', () => {
    const children = buildChildRequests({
      entry: makeEntry({ signal_id: 42 }),
      stopLoss: '60000',
    });
    expect(children[0].signal_id).toBe(42);
  });
});

// ---------------------------------------------------------------------------
// executeOrderChain
// ---------------------------------------------------------------------------

describe('executeOrderChain', () => {
  function makeDeps(opts: { entryOrderId?: number } = {}) {
    const orderIds = [opts.entryOrderId ?? 100, 101, 102];
    let idx = 0;
    const prepare = vi.fn(async (_req: PrepareNewRequest) => {
      void _req;
      return makePrepareResponse(orderIds[idx++ % orderIds.length]);
    });
    const sign = vi.fn(async (_td: TypedData) => {
      void _td;
      return '0x01' + 'a'.repeat(130);
    });
    const submit = vi.fn(
      async ({ orderId }: { orderId: number; signature: string }): Promise<ChainStepResult> => ({
        order_id: orderId,
        status: 'filled',
        exchange_order_id: `ex-${orderId}`,
      }),
    );
    const progress: ChainProgress[] = [];
    const onStep = vi.fn((p: ChainProgress) => progress.push(p));
    return { prepare, sign, submit, onStep, progress };
  }

  it('single-leg entry executes 1 prepare/sign/submit and reports one progress tick', async () => {
    const deps = makeDeps({ entryOrderId: 500 });
    const results = await executeOrderChain({ entry: makeEntry() }, deps);
    expect(deps.prepare).toHaveBeenCalledTimes(1);
    expect(deps.sign).toHaveBeenCalledTimes(1);
    expect(deps.submit).toHaveBeenCalledTimes(1);
    expect(results).toHaveLength(1);
    expect(results[0].order_id).toBe(500);
    expect(deps.progress).toEqual([{ step: 1, total: 1, label: 'entry' }]);
  });

  it('with SL + TP executes 3 legs and threads entry.order_id as parent', async () => {
    const deps = makeDeps();
    const results = await executeOrderChain(
      { entry: makeEntry(), stopLoss: '60000', takeProfit: '70000' },
      deps,
    );
    expect(deps.prepare).toHaveBeenCalledTimes(3);
    expect(results).toHaveLength(3);
    // 2nd and 3rd prepare calls carry parent_order_id=entry.order_id (100).
    const slReq = deps.prepare.mock.calls[1][0];
    const tpReq = deps.prepare.mock.calls[2][0];
    expect(slReq.parent_order_id).toBe(100);
    expect(tpReq.parent_order_id).toBe(100);
    expect(deps.progress).toEqual([
      { step: 1, total: 3, label: 'entry' },
      { step: 2, total: 3, label: 'stop_loss' },
      { step: 3, total: 3, label: 'take_profit' },
    ]);
  });

  it('throws on first failing leg and does not prepare subsequent legs', async () => {
    const deps = makeDeps();
    // Make sign fail on the 2nd leg (SL).
    deps.sign.mockImplementationOnce(async () => '0x01' + 'a'.repeat(130));
    deps.sign.mockImplementationOnce(async () => {
      throw new Error('user rejected');
    });
    await expect(
      executeOrderChain({ entry: makeEntry(), stopLoss: '60000', takeProfit: '70000' }, deps),
    ).rejects.toThrow('user rejected');
    // Entry + SL preparing happened, but TP prepare did NOT.
    expect(deps.prepare).toHaveBeenCalledTimes(2);
  });

  // PR P1-fix.B1/F2 — terminal-status entry short-circuit.
  it.each<['rejected' | 'expired' | 'cancelled']>([
    ['rejected'],
    ['expired'],
    ['cancelled'],
  ])(
    'aborts the chain when entry submit returns status=%s',
    async (badStatus) => {
      const deps = makeDeps();
      deps.submit.mockImplementationOnce(async ({ orderId }) => ({
        order_id: orderId,
        status: badStatus,
      }));
      await expect(
        executeOrderChain({ entry: makeEntry(), stopLoss: '60000', takeProfit: '70000' }, deps),
      ).rejects.toThrow(new RegExp(badStatus));
      // Only the entry leg was prepared/signed/submitted — children
      // never touched, so no orphan reduce_only orders persist.
      expect(deps.prepare).toHaveBeenCalledTimes(1);
      expect(deps.sign).toHaveBeenCalledTimes(1);
      expect(deps.submit).toHaveBeenCalledTimes(1);
    },
  );
});
