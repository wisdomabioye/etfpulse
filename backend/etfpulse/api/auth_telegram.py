"""Telegram WebApp `initData` HMAC verifier.

PR D.5.1 — verifies that a payload presented as Telegram WebApp
initData was genuinely signed by Telegram for our bot token. The
verifier is the gate between an anonymous web caller and a JWT
issued for the bound `tg_user_id`.

Spec — Telegram WebApp `initData` integrity (per the WebApp docs at
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):

  1. The `initData` arrives as a query-string-encoded payload. One key
     in the payload is `hash` — the integrity tag.
  2. Drop `hash` from the payload, build `data_check_string` by
     sorting the remaining keys ASCII-ascending and joining them as
     `f"{k}={v}"` with `\n` separators.
  3. Compute `secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token.encode())`.
  4. Compute `expected = HMAC_SHA256(key=secret_key, msg=data_check_string.encode()).hexdigest()`.
  5. Compare `expected` to the `hash` field — constant-time.

The verifier additionally enforces `auth_date` freshness:

  - `auth_date` is a unix-second timestamp Telegram includes in
    initData. We reject if `now - auth_date > max_age_seconds`. A
    captured initData payload thus has a bounded replay window.

No DB access. Pure function. Testable from raw fixtures.

This module imports `hmac`/`hashlib` from stdlib — NOT eth_account,
NOT any signing primitive. Anti-drift rule 27 is path-scoped to
`adapters/sodex/*`; this module is unaffected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl

import structlog
from fastapi import HTTPException, status

log = structlog.get_logger(__name__)


class WebAppVerifyError(HTTPException):
    """400 raised when initData parsing or HMAC verification fails.

    Detail strings are deliberately specific — these failures land
    BEFORE any user identity is established and a legitimate caller
    with a clock-skew bug benefits from knowing exactly which check
    failed. An attacker probing would need to break HMAC-SHA256 over
    a bot_token they don't have to bypass.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def verify_webapp_init_data(
    raw: str,
    *,
    bot_token: str,
    max_age_seconds: int,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify Telegram WebApp `initData` HMAC + freshness.

    Returns the parsed `user` dict on success. Raises `WebAppVerifyError`
    on any failure.

    `raw` is the query-string-encoded initData string the FE pulls from
    `window.Telegram.WebApp.initData`. The caller must NOT pre-decode
    the URL-encoding — `parse_qsl` does that and `data_check_string`
    needs the percent-decoded form per Telegram's spec.

    `now` is an injection point for tests; defaults to `time.time()`.
    """
    if not bot_token:
        # Server misconfig — Telegram bot isn't enabled but the route
        # was reached. Defensive belt; the route's `is_bot_enabled`
        # gate should have 404'd before this branch fires.
        log.error("webapp_verify_no_bot_token")
        raise WebAppVerifyError("server: bot not configured")

    if not isinstance(raw, str) or not raw:
        raise WebAppVerifyError("missing init_data")

    # `parse_qsl(keep_blank_values=True)` matches Telegram's encoding:
    # all values are URL-encoded, blank values may legitimately appear
    # (e.g., empty `query_id` on some launch sources). `strict_parsing`
    # OFF — Telegram's encoding occasionally diverges from strict RFC
    # 3986 (URL-encoded `+` characters) and rejecting on those would
    # break legitimate payloads.
    try:
        pairs = parse_qsl(raw, keep_blank_values=True)
    except (ValueError, UnicodeDecodeError) as exc:
        log.info("webapp_verify_parse_failed", error=str(exc))
        raise WebAppVerifyError("malformed init_data") from exc

    payload = dict(pairs)
    received_hash = payload.pop("hash", None)
    if not received_hash:
        raise WebAppVerifyError("missing hash field")

    # `auth_date` is mandatory per the WebApp spec. Reject if absent
    # OR not a parseable unix-second integer.
    auth_date_raw = payload.get("auth_date")
    if not auth_date_raw:
        raise WebAppVerifyError("missing auth_date field")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise WebAppVerifyError("malformed auth_date") from exc

    # Freshness window. `now - auth_date` can be negative (future
    # auth_date from clock skew); we only reject when it's too far
    # in the past. A small leeway tolerates 1-2 second skew without
    # complicating the test surface; the 600-second default already
    # absorbs most skew scenarios.
    current = now if now is not None else time.time()
    age = current - auth_date
    if age > max_age_seconds:
        log.info("webapp_verify_auth_date_expired", age=age, max_age=max_age_seconds)
        raise WebAppVerifyError("init_data expired")

    # Build the data_check_string. Sort by ASCII-ascending key. Skip
    # the already-popped `hash`. Each line is `f"{k}={v}"`.
    data_check_string = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))

    # Two-stage HMAC per the spec. `b"WebAppData"` is the literal key
    # — same across all bots, not a per-bot value.
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    # Constant-time compare. `hmac.compare_digest` accepts both str
    # and bytes; we use str on both sides (lowercase hex).
    if not hmac.compare_digest(expected, received_hash):
        log.info("webapp_verify_hash_mismatch")
        raise WebAppVerifyError("invalid hash")

    # Extract the user field. Telegram encodes it as a JSON string
    # inside the query parameter. Required for our purposes (no
    # `tg_user_id` → no JWT to mint).
    user_raw = payload.get("user")
    if not user_raw:
        raise WebAppVerifyError("missing user field")
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise WebAppVerifyError("malformed user field") from exc
    if not isinstance(user, dict) or "id" not in user:
        raise WebAppVerifyError("malformed user field")
    if not isinstance(user["id"], int) or user["id"] <= 0:
        raise WebAppVerifyError("malformed user id")

    log.info("webapp_verify_ok", tg_user_id=user["id"])
    return user
