/**
 * Execute page — place + manage SoDEX orders.
 *
 * This file is the slim composition shell; the feature pieces live under
 * `components/execution/`:
 *   - TradeHeader / ModePill / RequestLiveBlock — header + mode + go-live.
 *   - WalletGate — missing-wallet onboarding + wallet/session mismatch banner.
 *   - ApiKeySection — SoDEX account/key setup.
 *   - OrderForm — prepare → sign → submit ceremony (incl. SL/TP chain).
 *   - PositionsPanel / OrdersPanel — live positions + orders with close/cancel.
 *
 * The backend NEVER holds keys or auto-trades — every order is prepared by the
 * backend, signed in the user's wallet (wagmi/viem), and submitted.
 */

import { Navigate, useLocation, useSearchParams } from 'react-router-dom';

import { ApiKeySection } from '../components/execution/ApiKeySection';
import { ErrorBanner } from '../components/execution/ErrorBanner';
import { OrderFormSection } from '../components/execution/OrderForm';
import { OrdersTableSection } from '../components/execution/OrdersPanel';
import { PositionsSection } from '../components/execution/PositionsPanel';
import { SignalPrefillBanner } from '../components/execution/SignalPrefillBanner';
import { TradeHeader } from '../components/execution/TradeHeader';
import { WalletMissingNotice } from '../components/execution/WalletGate';
import { Breadcrumb } from '../components/layout';
import { useAuth } from '../auth/useAuth';
import { useSignal } from '../api/queries';
import { useWalletMe } from '../hooks/useExecution';

// Re-exported for the #78.4 / WalletMissing regression tests, which import
// these from `pages/Execute` (the components now live in WalletGate).
export { WalletMissingNotice, WalletMissingUnavailable } from '../components/execution/WalletGate';

export function Execute() {
  const { isAuthed } = useAuth();
  const location = useLocation();
  if (!isAuthed) {
    // SIG2X.3 — preserve the intended URL (incl. ?signal_id=N) across the
    // auth bounce so /signals/N "Execute this signal" and Telegram alert
    // deep-links land on the prefilled form after SIWE. The Login page reads
    // `location.state.from` and navigates there on success.
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <ExecuteInner />;
}

function ExecuteInner() {
  const me = useWalletMe();

  // SIG2X — read `?signal_id=N` at the page level so the prefill banner can
  // render full-width above the trade grid and the same fetched signal feeds
  // the form's prefill effect. Reject 0, negatives, leading-zero, non-digits.
  const [searchParams] = useSearchParams();
  const signalIdParam = searchParams.get('signal_id');
  const signalId =
    signalIdParam && /^[1-9]\d*$/.test(signalIdParam) ? Number(signalIdParam) : undefined;
  const signalQuery = useSignal(signalId);

  if (me.isLoading)
    return (
      <PageShell>
        <p className="text-t2">Loading…</p>
      </PageShell>
    );
  if (me.error) {
    return (
      <PageShell>
        <ErrorBanner error={me.error} fallback="Failed to load account state." />
      </PageShell>
    );
  }
  if (!me.data) return null;
  const account = me.data;

  // Trade-ready = SoDEX recognises this wallet (account_id) AND at least one
  // venue's API key is bound. Until then there's nothing to trade, so we DON'T
  // render the order form or the (empty) positions/orders panels — just the
  // compact setup notice.
  const tradeReady = !!(
    account.sodex_account_id &&
    (account.sodex_spot_api_key_name || account.sodex_perps_api_key_name)
  );

  return (
    <PageShell>
      <TradeHeader me={account} />

      {/* Telegram-WebApp users land here with a JWT but no bound wallet. */}
      {account.wallet_address === null && <WalletMissingNotice />}

      {tradeReady ? (
        <>
          {/* A not-yet-bound venue surfaces a bind prompt; null once bound. */}
          <ApiKeySection me={account} />

          {signalId !== undefined && (
            <SignalPrefillBanner
              signalId={signalId}
              signal={signalQuery.data}
              isLoading={signalQuery.isLoading}
              isError={signalQuery.isError}
            />
          )}

          {/* Prototype 2-column trade layout: order form left (sticky), live
              positions + orders right. */}
          <div className="grid grid-cols-1 lg:grid-cols-[380px_1fr] gap-6 items-start">
            <div className="lg:sticky lg:top-[96px]">
              <OrderFormSection me={account} signalId={signalId} signal={signalQuery.data} />
            </div>
            <div className="space-y-6 min-w-0">
              <PositionsSection />
              <OrdersTableSection />
            </div>
          </div>
        </>
      ) : (
        // Not set up on SoDEX yet — compact setup notice only (no empty
        // order/position panels). ApiKeySection renders the account/key
        // guidance, or null when the wallet itself isn't bound.
        account.wallet_address !== null && (
          <div className="max-w-[460px]">
            <ApiKeySection me={account} />
          </div>
        )
      )}
    </PageShell>
  );
}

function PageShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="max-w-[1280px] mx-auto px-6 pt-8 pb-12 space-y-6">
      <Breadcrumb items={[{ label: 'ETFPulse', path: '/' }, { label: 'Trade' }]} />
      {children}
    </div>
  );
}
