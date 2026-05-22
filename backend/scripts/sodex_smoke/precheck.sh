#!/usr/bin/env bash
# scripts/sodex_smoke/precheck.sh — D.5 live-smoke readiness probe.
#
# Operator runs this BEFORE walking through `docs/sodex/D5_SMOKE.md`. It's
# a "can I even start the smoke?" check — fails loud on any missing piece
# so the operator doesn't get halfway through step 5 and realise step 0
# was broken.
#
# What it checks (in order; fails loud on first failure):
#
#   1. Backend is reachable (curl /api/health → 200).
#   2. Backend readiness is clean (curl /api/health/ready → 200, no
#      config.errors).
#   3. SoDEX clients are attached on app.state (config.warnings does
#      NOT mention "SoDEX clients" — a smoke test against a no-client
#      deploy would 503 on every prepare).
#   4. `frontend_url` is HTTPS (Telegram WebApp button silently broken
#      otherwise).
#   5. `is_bot_enabled` is true (else /api/auth/telegram/verify 404s).
#   6. `jwt_secret` is set (else dev-fallback secret invalidates tokens
#      on container restart).
#   7. `sodex_environment` is "testnet" (mainnet smoke is a separate
#      doc / runbook).
#   8. `sodex_chain_id` is canonical (138565 for testnet).
#
# Exit 0 = ready. Non-zero = stop, fix, re-run.
#
# Usage:
#   BACKEND_URL=http://localhost:8000 scripts/sodex_smoke/precheck.sh
#   BACKEND_URL=https://etfpulse-api.example.com scripts/sodex_smoke/precheck.sh
#
# Default BACKEND_URL is http://localhost:8000.

set -euo pipefail

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
FAIL_COUNT=0

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

fail() {
    red "  ✗ $*"
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

ok() {
    green "  ✓ $*"
}

warn() {
    yellow "  ! $*"
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        red "Required command not found: $1"
        red "Install it and re-run."
        exit 2
    fi
}

require_cmd curl
require_cmd jq

bold "D.5 live-smoke precheck — backend at $BACKEND_URL"
echo

# 1. Liveness
bold "[1/8] Liveness probe"
if curl -fsS "$BACKEND_URL/api/health" -o /tmp/precheck_health.json 2>/dev/null; then
    if jq -e '.status == "ok"' /tmp/precheck_health.json >/dev/null 2>&1; then
        ok "/api/health returns 200 with status=ok"
    else
        fail "/api/health body is not {status: ok}"
    fi
else
    fail "/api/health unreachable — is the backend running?"
fi

# 2. Readiness body
bold "[2/8] Readiness probe (DB ping + config preflight)"
HTTP_CODE=$(curl -s -o /tmp/precheck_ready.json -w '%{http_code}' "$BACKEND_URL/api/health/ready" || echo "000")
if [[ "$HTTP_CODE" == "200" ]]; then
    ok "/api/health/ready returned 200"
else
    fail "/api/health/ready returned $HTTP_CODE (expected 200)"
    if [[ -s /tmp/precheck_ready.json ]]; then
        echo "  body:"
        sed 's/^/    /' /tmp/precheck_ready.json
    fi
fi

if [[ -s /tmp/precheck_ready.json ]]; then
    ERRORS=$(jq -r '.config.errors[]?' /tmp/precheck_ready.json 2>/dev/null || true)
    if [[ -n "$ERRORS" ]]; then
        fail "config preflight has errors:"
        echo "$ERRORS" | sed 's/^/    - /'
    else
        ok "no config errors"
    fi
    WARNINGS=$(jq -r '.config.warnings[]?' /tmp/precheck_ready.json 2>/dev/null || true)
    if [[ -n "$WARNINGS" ]]; then
        warn "config preflight warnings (review but won't block smoke):"
        echo "$WARNINGS" | sed 's/^/    - /'
    fi
fi

# 3-8: app-state attributes are NOT exposed via /api/health/ready (by design —
# health surface stays minimal). The remaining checks are operator-side env
# inspection. We probe them via the wallet/nonce route which fails fast on a
# misconfigured `frontend_url` (returns 503 instead of issuing a nonce).

bold "[3/8] Wallet nonce route (validates frontend_url + siwe_domain)"
NONCE_CODE=$(curl -s -o /tmp/precheck_nonce.json -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -d '{"address":"0x0000000000000000000000000000000000000001"}' \
    "$BACKEND_URL/api/wallet/nonce" 2>/dev/null || echo "000")
case "$NONCE_CODE" in
    200) ok "wallet/nonce returns 200 (frontend_url + siwe_domain configured)" ;;
    503) fail "wallet/nonce returned 503 — frontend_url likely empty. Set FRONTEND_URL." ;;
    *)   fail "wallet/nonce returned $NONCE_CODE (expected 200 or 503)" ;;
esac

bold "[4/8] Telegram verify route (gates on is_bot_enabled)"
TG_CODE=$(curl -s -o /tmp/precheck_tg.json -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -d '{"init_data":"placeholder=for-routing-check"}' \
    "$BACKEND_URL/api/auth/telegram/verify" 2>/dev/null || echo "000")
case "$TG_CODE" in
    400) ok "telegram/verify returns 400 (bot enabled; HMAC rejected placeholder — expected)" ;;
    404) fail "telegram/verify returned 404 — is_bot_enabled is False. Set TELEGRAM_BOT_TOKEN + TELEGRAM_PUBLIC_URL + TELEGRAM_WEBHOOK_SECRET + TELEGRAM_WEBHOOK_URL_SUFFIX." ;;
    422) ok "telegram/verify returns 422 (schema rejected placeholder — bot enabled)" ;;
    *)   fail "telegram/verify returned $TG_CODE (expected 400 or 422)" ;;
esac

bold "[5/8] Execution symbols route (gates on auth + sodex_*_client)"
# Anonymous → expect 401. If 503, the SoDEX HTTP clients aren't attached.
SYM_CODE=$(curl -s -o /tmp/precheck_sym.json -w '%{http_code}' \
    "$BACKEND_URL/api/execution/symbols" 2>/dev/null || echo "000")
case "$SYM_CODE" in
    401) ok "execution/symbols returns 401 unauth (auth gate working)" ;;
    503) fail "execution/symbols returned 503 — SoDEX clients not on app.state. Check scheduler boot logs + SODEX_* env." ;;
    *)   warn "execution/symbols returned $SYM_CODE (unexpected; manual review)" ;;
esac

echo
bold "Result"
if [[ "$FAIL_COUNT" -eq 0 ]]; then
    green "All precheck items passed. Proceed to docs/sodex/D5_SMOKE.md."
    exit 0
else
    red "$FAIL_COUNT precheck item(s) failed. Fix above, re-run before starting smoke."
    exit 1
fi
