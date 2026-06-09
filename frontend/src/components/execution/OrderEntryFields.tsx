/**
 * Presentational entry-field cluster for the order form: venue + side toggle +
 * asset + type/TIF + size/price + leverage. State lives in the parent
 * `OrderForm`; this is a controlled view of it.
 */

import type { Venue } from '../../api/execution';
import { sideDisplay } from '../../lib/orderSide';
import { BigToggle, Field, FieldGroup, Seg } from '../ui';

interface OrderEntryFieldsProps {
  venue: Venue;
  setVenue: (v: Venue) => void;
  venueOptions: ReadonlyArray<readonly [string, Venue]>;
  side: 'buy' | 'sell';
  setSide: (s: 'buy' | 'sell') => void;
  asset: string;
  setAsset: (a: string) => void;
  assetOptions: string[];
  noSymbols: boolean;
  orderType: 'limit' | 'market';
  setOrderType: (t: 'limit' | 'market') => void;
  tif: 'gtc' | 'ioc' | 'gtx';
  setTif: (t: 'gtc' | 'ioc' | 'gtx') => void;
  size: string;
  setSize: (s: string) => void;
  price: string;
  setPrice: (p: string) => void;
  leverage: string;
  setLeverage: (l: string) => void;
  isPerps: boolean;
}

const SELECT_CLASS =
  'w-full bg-bg-1 text-t1 border border-line-3 rounded-sm px-2.5 py-2 text-[12.5px] font-mono';
const INPUT_CLASS = `${SELECT_CLASS} tabular-nums`;

export function OrderEntryFields({
  venue,
  setVenue,
  venueOptions,
  side,
  setSide,
  asset,
  setAsset,
  assetOptions,
  noSymbols,
  orderType,
  setOrderType,
  tif,
  setTif,
  size,
  setSize,
  price,
  setPrice,
  leverage,
  setLeverage,
  isPerps,
}: OrderEntryFieldsProps) {
  const buySide = sideDisplay(venue, 'buy');
  const sellSide = sideDisplay(venue, 'sell');
  return (
    <>
      {/* venue */}
      <FieldGroup label="Venue">
        <Seg options={venueOptions} value={venue} onChange={(v) => setVenue(v)} />
      </FieldGroup>

      {/* side — venue-aware: perps is directional Long/Short, spot is a plain
          Buy/Sell of a holding (no shorting). The underlying `side` value stays
          buy/sell across venues; only the label changes. */}
      <FieldGroup label="Side">
        <div className="flex gap-2">
          <BigToggle
            on={side === 'buy'}
            token={buySide.token}
            glyph={buySide.glyph || undefined}
            label={buySide.label}
            onClick={() => setSide('buy')}
          />
          <BigToggle
            on={side === 'sell'}
            token={sellSide.token}
            glyph={sellSide.glyph || undefined}
            label={sellSide.label}
            onClick={() => setSide('sell')}
          />
        </div>
      </FieldGroup>

      {/* asset */}
      <Field label="Asset">
        <select
          value={asset}
          onChange={(e) => setAsset(e.target.value)}
          disabled={noSymbols}
          className={SELECT_CLASS}
        >
          {noSymbols && <option>— no symbols cached —</option>}
          {assetOptions.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </Field>

      {/* type + tif */}
      <div className="grid grid-cols-2 gap-3">
        <Field label="Order type">
          <select
            value={orderType}
            onChange={(e) => setOrderType(e.target.value as 'limit' | 'market')}
            className={SELECT_CLASS}
          >
            <option value="limit">Limit</option>
            <option value="market">Market</option>
          </select>
        </Field>
        <Field label="Time in force">
          <select
            value={tif}
            onChange={(e) => setTif(e.target.value as 'gtc' | 'ioc' | 'gtx')}
            className={SELECT_CLASS}
          >
            <option value="gtc">GTC</option>
            <option value="ioc">IOC</option>
            <option value="gtx">GTX</option>
          </select>
        </Field>
      </div>

      {/* size + price */}
      <div className="grid grid-cols-2 gap-3">
        <Field label="Size">
          <input
            type="text"
            inputMode="decimal"
            value={size}
            onChange={(e) => setSize(e.target.value)}
            placeholder="0.01"
            required
            className={INPUT_CLASS}
          />
        </Field>
        <Field label={orderType === 'market' ? 'Price (mkt)' : 'Limit price'}>
          <input
            type="text"
            inputMode="decimal"
            value={orderType === 'market' ? '—' : price}
            onChange={(e) => setPrice(e.target.value)}
            disabled={orderType === 'market'}
            placeholder="65000"
            required={orderType === 'limit'}
            className={`${INPUT_CLASS} disabled:text-t4`}
          />
        </Field>
      </div>

      {/* leverage (perps) */}
      {isPerps && (
        <Field label={`Leverage · ${leverage}×`}>
          <input
            type="range"
            min={1}
            max={20}
            value={leverage}
            onChange={(e) => setLeverage(e.target.value)}
            className="w-full accent-acc"
          />
        </Field>
      )}
    </>
  );
}
