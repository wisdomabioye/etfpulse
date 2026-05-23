"""Anti-drift rule 32 — every authed route uses `get_current_user`.

PR D.5.5 — pins the IDOR-defense contract. The risk:

  Future PR adds a new route at, say, `/api/foo/{user_id}` that loads
  data via `Order.user_id == user_id`. If `user_id` comes from the
  PATH (or body, or query) instead of from `get_current_user`, ANY
  authenticated user can read any other user's data by guessing the
  ID. This is the textbook IDOR class.

The rule:

  1. Every route that needs user identity MUST resolve it via
     `Depends(get_current_user)` (or `get_current_user_unbound` —
     the wallet-less variant on the same auth foundation).
  2. The set of routes that need user identity is an explicit
     allowlist in `EXPECTED_AUTHED_ROUTES`. Adding an authed route
     means adding it here too — a deliberate, reviewed act.
  3. Public + admin + webhook routes are explicitly listed in
     `EXPECTED_NON_AUTHED_ROUTES` so a future "I forgot the auth
     dep" PR fails the "every route must be in one of the two sets"
     contract.

Implementation: walk `app.routes`, build the dependency tree per route
(FastAPI exposes this on `APIRoute.dependant`), check whether either
of the two auth deps appears anywhere in the tree, and reconcile
against the expected sets.

When this test fails, the failure message tells you EXACTLY which
list to update — there is no judgment call at PR review time. Either
the route is authed (add to `EXPECTED_AUTHED_ROUTES`) or it's not (add
to `EXPECTED_NON_AUTHED_ROUTES`). The middle case ("authed but missing
the dep") is the bug.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from etfpulse.api.auth import get_current_user, get_current_user_unbound
from etfpulse.app import create_app

# Routes that MUST resolve the caller via `get_current_user[_unbound]`.
# Format: `(method, path)` — exact match required.
EXPECTED_AUTHED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # Wallet introspection + binding (D.4.2)
        ("GET", "/api/wallet/me"),
        ("POST", "/api/wallet/api-key"),
        # PR #185 — paper-trade users request live trading.
        ("POST", "/api/wallet/request-live"),
        # Execution surface (D.4.3)
        ("POST", "/api/execution/prepare"),
        ("POST", "/api/execution/submit/{order_id}"),
        ("POST", "/api/execution/prepare-cancel/{order_id}"),
        ("POST", "/api/execution/submit-cancel/{order_id}"),
        ("GET", "/api/execution/orders"),
        ("GET", "/api/execution/orders/{order_id}"),
        ("GET", "/api/execution/positions"),
        ("GET", "/api/execution/symbols"),
    }
)

# Routes that are deliberately NOT user-authed. Each entry has a one-line
# justification so a future reader (or refactor) knows the policy.
EXPECTED_NON_AUTHED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # Health: never authed (orchestrator probes, not user calls).
        ("GET", "/api/health"),
        ("HEAD", "/api/health"),
        ("GET", "/api/health/ready"),
        ("HEAD", "/api/health/ready"),
        # SIWE first step (D.4.2): no auth — the route ISSUES the
        # nonce that proves identity in the next call.
        ("POST", "/api/wallet/nonce"),
        # SIWE bind (D.4.2 / D.5 Option A): dual-path — accepts an
        # optional inbound JWT but does NOT require it. The path-A
        # (anonymous first-bind) branch must work without auth.
        ("POST", "/api/wallet/verify"),
        # Telegram WebApp bind (D.5.1): no auth — initData HMAC is
        # the identity proof. Mints the first JWT for a tg-bound user.
        ("POST", "/api/auth/telegram/verify"),
        # Public reads (signal track record + dashboards). Show
        # aggregate stats, not per-user state. Identity not relevant.
        ("GET", "/api/signals"),
        ("GET", "/api/signals/{signal_id}"),
        ("GET", "/api/dashboard/stats"),
        ("GET", "/api/regime"),
        ("GET", "/api/track-record"),
        ("GET", "/api/track-record/calibration"),
        ("GET", "/api/track-record/per-detector"),
        ("GET", "/api/analytics/breakdown"),
        ("GET", "/api/prices/spot"),
        # Telegram webhook: gated by url-suffix + bot-enabled + secret
        # chain (`verify_telegram_secret`), not user JWT. The "caller"
        # is Telegram itself, not a user.
        ("POST", "/api/telegram/webhook/{suffix}"),
        # Admin surface: gated by `require_admin_key`, not user JWT.
        # Operator identity is a single shared secret, not per-user.
        ("POST", "/api/admin/signals/trigger"),
        ("POST", "/api/admin/signals/eval-outcomes"),
        ("POST", "/api/admin/signals/retry-ai"),
        ("GET", "/api/admin/signals/{signal_id}/delivery-trace"),
        ("POST", "/api/admin/telegram/rotate-webhook-secret"),
        ("POST", "/api/admin/telegram/groups/{group_id}/heal-migration"),
        ("GET", "/api/admin/metrics"),
        ("POST", "/api/admin/execution/halt"),
        ("POST", "/api/admin/execution/resume"),
        ("POST", "/api/admin/users/{user_id}/paper-trade"),
        ("POST", "/api/admin/users/{user_id}/unbind-wallet"),
        ("POST", "/api/admin/sodex/symbols/refresh"),
    }
)


def _flatten_deps(d: Dependant) -> Iterable[Dependant]:
    """Yield `d` and every sub-dependant in its tree.

    FastAPI's `Dependant.dependencies` is the list of `Depends(...)`
    declarations on the route handler. Each is itself a Dependant with
    its OWN dependencies (e.g., `get_current_user` depends on
    `get_db_session`). We walk recursively.
    """
    yield d
    for sub in d.dependencies:
        yield from _flatten_deps(sub)


def _has_auth_dep(route: APIRoute) -> bool:
    """Return True if the route's dependency tree resolves the user
    via `get_current_user` or `get_current_user_unbound`."""
    for dep in _flatten_deps(route.dependant):
        if dep.call is get_current_user or dep.call is get_current_user_unbound:
            return True
    return False


def _all_app_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Collect (method, path) pairs for every APIRoute on the app.

    `HEAD` is implicit on `GET` endpoints — FastAPI registers it
    automatically. We materialise both so the expected sets match
    what the framework actually exposes.
    """
    out: set[tuple[str, str]] = set()
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        for method in r.methods or set():
            out.add((method, r.path))
    return out


def test_authed_route_set_is_exact():
    """The set of routes resolving the user via `get_current_user*`
    EQUALS `EXPECTED_AUTHED_ROUTES`. If this fails:

      - Extra in actual → you added a route without updating the
        expected set. Add `(method, path)` to `EXPECTED_AUTHED_ROUTES`
        (or `EXPECTED_NON_AUTHED_ROUTES` if it's now non-authed).
      - Missing from actual → you removed `Depends(get_current_user)`
        from an authed route. Restore the dep OR move the route to
        `EXPECTED_NON_AUTHED_ROUTES` with a justification.
    """
    app = create_app()
    actual_authed: set[tuple[str, str]] = set()
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        if not _has_auth_dep(r):
            continue
        for method in r.methods or set():
            actual_authed.add((method, r.path))

    extra = actual_authed - EXPECTED_AUTHED_ROUTES
    missing = EXPECTED_AUTHED_ROUTES - actual_authed
    assert not extra, (
        f"Routes that use get_current_user[_unbound] but aren't in "
        f"EXPECTED_AUTHED_ROUTES: {sorted(extra)}. "
        f"If they're correctly authed, add them to the set. If they "
        f"shouldn't be authed, remove the Depends() call."
    )
    assert not missing, (
        f"Routes in EXPECTED_AUTHED_ROUTES but missing the dep: "
        f"{sorted(missing)}. Did someone remove the Depends() call?"
    )


def test_every_route_is_classified():
    """Every APIRoute MUST appear in either `EXPECTED_AUTHED_ROUTES`
    or `EXPECTED_NON_AUTHED_ROUTES`. Routes outside both sets are
    unclassified — a future "added a route, forgot to think about
    auth" PR gets caught here.

    Failure means: add `(method, path)` to one of the two sets, with
    a one-line justification for `EXPECTED_NON_AUTHED_ROUTES` entries.
    """
    app = create_app()
    actual = _all_app_routes(app)
    classified = EXPECTED_AUTHED_ROUTES | EXPECTED_NON_AUTHED_ROUTES
    unclassified = actual - classified
    assert not unclassified, (
        f"Routes not declared in either EXPECTED_AUTHED_ROUTES or "
        f"EXPECTED_NON_AUTHED_ROUTES: {sorted(unclassified)}. "
        f"Add each to the appropriate set. Authed routes use "
        f"Depends(get_current_user); non-authed routes need a "
        f"justification comment."
    )


def test_no_route_in_both_sets():
    """`EXPECTED_AUTHED_ROUTES` and `EXPECTED_NON_AUTHED_ROUTES` are
    disjoint by construction. Catches a copy-paste mistake that
    would make `test_authed_route_set_is_exact` confusingly pass."""
    overlap = EXPECTED_AUTHED_ROUTES & EXPECTED_NON_AUTHED_ROUTES
    assert not overlap, f"Routes appear in BOTH expected sets: {sorted(overlap)}. Pick one."
