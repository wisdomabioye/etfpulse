/**
 * Top-level React ErrorBoundary (#78.15).
 *
 * Catches render-time + lifecycle errors anywhere below it. Renders a
 * graceful fallback with a Retry button instead of leaving the user
 * staring at a white screen.
 *
 * Primary use case: a lazy-imported chunk (`React.lazy(() => import())`
 * for Login + Execute + WalletProviders, see #78.5) fails to fetch.
 * `React.lazy` throws inside the Suspense boundary; without an
 * ErrorBoundary, the error bubbles to root and React unmounts the
 * entire tree.
 *
 * Why a class component: React's hook API has no equivalent of
 * `componentDidCatch` + `getDerivedStateFromError`. The class form is
 * the official + only way to define an error boundary as of React 19.
 *
 * Scope:
 *   - The boundary is mounted at the App root, so it catches errors in
 *     EVERY route. A more granular per-route boundary is a future
 *     refinement (would let one route's lazy fail without unmounting
 *     the nav).
 *   - The boundary catches RENDER-TIME errors. Async errors (promise
 *     rejections in event handlers, fetch errors) are NOT caught — the
 *     caller still has to handle those.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // Operator log only — no remote telemetry hook in V1. If a future
    // change adds Sentry / Datadog FE error tracking, wire it here.
    console.error('[ErrorBoundary]', error, errorInfo);
  }

  handleRetry = (): void => {
    // Reload is the bluntest possible recovery — it works for the most
    // common case (a lazy chunk fetch failed) by re-running the entire
    // module graph + cache lookup. A more surgical retry (reset state
    // + remount the failed subtree) would require knowing what failed,
    // which we don't.
    //
    // Why not just `setState({ hasError: false })`: that would unblock
    // re-render, but if React.lazy already cached the failure, the
    // next render re-throws immediately. Reload bypasses the cache.
    window.location.reload();
  };

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-bg-0 p-4 sm:p-6">
          <div className="max-w-md mx-auto mt-20 rounded-xl border border-amber-500/30 bg-amber-500/5 p-6 text-t1 space-y-4">
            <h1 className="text-xl font-semibold text-amber-200">
              Something went wrong loading this page
            </h1>
            <p className="text-t2 text-sm">
              The most common cause is a network blip while loading a code
              bundle. Refreshing the page usually fixes it.
            </p>
            {this.state.error && (
              <details className="text-t3 text-xs">
                <summary className="cursor-pointer text-t2">
                  Technical details
                </summary>
                <pre className="mt-2 whitespace-pre-wrap break-words">
                  {this.state.error.message}
                </pre>
              </details>
            )}
            <button
              type="button"
              onClick={this.handleRetry}
              className="inline-flex items-center justify-center px-4 py-2 rounded-md bg-text-1 text-bg-1 text-sm font-medium hover:bg-text-2 transition-colors"
            >
              Reload
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
