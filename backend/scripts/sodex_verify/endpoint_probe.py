"""V.2 — Read-only probe of SoDEX testnet endpoints.

Operator-only. Hits public + account-scoped GET endpoints on both venues,
captures anonymized response shapes to `tests/fixtures/sodex_endpoint_responses.json`.
The D.2 SoDEX adapters (split per venue) then assert their response parsers
against this fixture in CI.

What this script does NOT do:
  - Place orders. (V.3 if/when we want signed-write coverage.)
  - Sign anything. Read-only endpoints don't need it; the documented
    `X-API-*` headers are for write endpoints.
  - Touch a faucet. Read-only endpoints work on zero-balance wallets, and
    the gateway has no documented faucet endpoint. Funding happens via
    the testnet web UI if/when V.3 needs it.

Run from `backend/`:

    export SODEX_VERIFY_ADDRESS=0x...   # from gen_burner.py — only for
                                        # the `/accounts/{addr}/...` path
                                        # parameter; not for auth.
    uv run python scripts/sodex_verify/endpoint_probe.py \
        --out tests/fixtures/sodex_endpoint_responses.json

The probe never persists the private key — only the burner address is
read, and it's redacted from response bodies before they hit disk
(replaced with the literal `<BURNER_ADDR>` placeholder). Status codes,
top-level keys, and value types are what we want to pin; the specific
balance numbers are noise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

SPOT_BASE = "https://testnet-gw.sodex.dev/api/v1/spot"
PERPS_BASE = "https://testnet-gw.sodex.dev/api/v1/perps"

# Per-endpoint metadata: (name, method, url-template, optional-query, expected-auth).
# `url-template` uses {addr} for the burner address path-segment substitution.
# `expected-auth` is informational only — we never send auth headers from
# this probe; if a GET that we think is public 401s, the recorded status
# reveals that.
SPOT_TARGETS: list[tuple[str, str, str, dict[str, str] | None]] = [
    ("spot_markets_symbols", "GET", f"{SPOT_BASE}/markets/symbols", None),
    ("spot_markets_coins", "GET", f"{SPOT_BASE}/markets/coins", None),
    ("spot_markets_tickers", "GET", f"{SPOT_BASE}/markets/tickers", None),
    ("spot_markets_mini_tickers", "GET", f"{SPOT_BASE}/markets/miniTickers", None),
    ("spot_markets_book_tickers", "GET", f"{SPOT_BASE}/markets/bookTickers", None),
    ("spot_account_balances", "GET", f"{SPOT_BASE}/accounts/{{addr}}/balances", None),
    ("spot_account_state", "GET", f"{SPOT_BASE}/accounts/{{addr}}/state", None),
    ("spot_account_orders_open", "GET", f"{SPOT_BASE}/accounts/{{addr}}/orders", None),
    ("spot_account_fee_rate", "GET", f"{SPOT_BASE}/accounts/{{addr}}/fee-rate", None),
    ("spot_account_api_keys", "GET", f"{SPOT_BASE}/accounts/{{addr}}/api-keys", None),
]

PERPS_TARGETS: list[tuple[str, str, str, dict[str, str] | None]] = [
    ("perps_markets_symbols", "GET", f"{PERPS_BASE}/markets/symbols", None),
    ("perps_markets_coins", "GET", f"{PERPS_BASE}/markets/coins", None),
    ("perps_markets_tickers", "GET", f"{PERPS_BASE}/markets/tickers", None),
    ("perps_markets_mark_prices", "GET", f"{PERPS_BASE}/markets/mark-prices", None),
    ("perps_account_balances", "GET", f"{PERPS_BASE}/accounts/{{addr}}/balances", None),
    ("perps_account_state", "GET", f"{PERPS_BASE}/accounts/{{addr}}/state", None),
    ("perps_account_orders_open", "GET", f"{PERPS_BASE}/accounts/{{addr}}/orders", None),
    ("perps_account_positions", "GET", f"{PERPS_BASE}/accounts/{{addr}}/positions", None),
    ("perps_account_fee_rate", "GET", f"{PERPS_BASE}/accounts/{{addr}}/fee-rate", None),
    ("perps_account_api_keys", "GET", f"{PERPS_BASE}/accounts/{{addr}}/api-keys", None),
]

# ---------------------------------------------------------------------------
# Probe execution
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    name: str
    method: str
    url: str
    status: int | None
    elapsed_ms: int | None
    response: Any = None
    error: str | None = None
    headers_sent: dict[str, str] = field(default_factory=dict)
    response_content_type: str | None = None


def _redact(obj: Any, address: str) -> Any:
    """Recursively replace burner address (case-insensitive) with placeholder."""
    addr_lc = address.lower()
    pattern = re.compile(re.escape(addr_lc), re.IGNORECASE)
    placeholder = "<BURNER_ADDR>"

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: walk(val) for k, val in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            return pattern.sub(placeholder, v)
        return v

    return walk(obj)


async def _probe_one(
    client: httpx.AsyncClient,
    name: str,
    method: str,
    url_template: str,
    query: dict[str, str] | None,
    address: str,
) -> ProbeResult:
    url = url_template.replace("{addr}", address.lower())
    headers = {"Accept": "application/json"}
    try:
        resp = await client.request(method, url, params=query, headers=headers)
    except httpx.RequestError as exc:
        return ProbeResult(
            name=name,
            method=method,
            url=url.replace(address.lower(), "<BURNER_ADDR>"),
            status=None,
            elapsed_ms=None,
            error=f"{type(exc).__name__}: {exc}",
            headers_sent=headers,
        )

    try:
        body: Any = resp.json()
    except (ValueError, json.JSONDecodeError):
        # Some 4xx/5xx may return text/html — record raw text but truncated.
        body = {"_non_json_text_prefix": resp.text[:500]}

    return ProbeResult(
        name=name,
        method=method,
        url=url.replace(address.lower(), "<BURNER_ADDR>"),
        status=resp.status_code,
        elapsed_ms=int(resp.elapsed.total_seconds() * 1000),
        response=_redact(body, address),
        headers_sent=headers,
        response_content_type=resp.headers.get("content-type"),
    )


async def main_async(out_path: str, address: str, timeout_s: float) -> int:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        print(
            f"ERROR: SODEX_VERIFY_ADDRESS={address!r} is not a 20-byte EVM address.",
            file=sys.stderr,
        )
        return 2

    targets = SPOT_TARGETS + PERPS_TARGETS
    results: list[ProbeResult] = []

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        for name, method, url_template, query in targets:
            print(f"  -> {name} ... ", end="", flush=True, file=sys.stderr)
            result = await _probe_one(client, name, method, url_template, query, address)
            status_str = str(result.status) if result.status is not None else f"ERR {result.error}"
            print(status_str, file=sys.stderr)
            results.append(result)

    output = {
        "schema_version": "v1",
        "generated_by": "scripts/sodex_verify/endpoint_probe.py",
        "burner_address_placeholder": "<BURNER_ADDR>",
        "probes": [
            {
                "name": r.name,
                "method": r.method,
                "url": r.url,
                "status": r.status,
                "elapsed_ms": r.elapsed_ms,
                "response_content_type": r.response_content_type,
                "headers_sent": r.headers_sent,
                "response": r.response,
                "error": r.error,
            }
            for r in results
        ],
    }

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, sort_keys=False)
        fh.write("\n")

    n_ok = sum(1 for r in results if r.status is not None and 200 <= r.status < 300)
    print(
        f"\nDone. {n_ok}/{len(results)} probes returned 2xx. Wrote {out_path}.",
        file=sys.stderr,
    )
    print(
        "  Review the fixture: non-2xx responses are intentionally preserved "
        "(they pin the gateway's actual error shape too).",
        file=sys.stderr,
    )
    return 0


_ADDRESS_CACHE_PATH = os.path.join(os.path.dirname(__file__), ".address")


def _resolve_address(cli_address: str | None) -> str | None:
    """Address resolution order: --address > env > cache file > None.

    The cache file is gitignored — it stores ONLY the address (public info)
    so repeat runs don't need an env export. The private key is never
    persisted, anywhere.
    """
    if cli_address:
        return cli_address.strip()
    env = os.environ.get("SODEX_VERIFY_ADDRESS", "").strip()
    if env:
        return env
    try:
        with open(_ADDRESS_CACHE_PATH, encoding="utf-8") as fh:
            cached = fh.read().strip()
            return cached or None
    except FileNotFoundError:
        return None


def _save_address(address: str) -> None:
    with open(_ADDRESS_CACHE_PATH, "w", encoding="utf-8") as fh:
        fh.write(address.strip() + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="SoDEX testnet read-only endpoint probe.")
    parser.add_argument(
        "--out",
        default="tests/fixtures/sodex_endpoint_responses.json",
        help="Output JSON path (relative to backend/).",
    )
    parser.add_argument(
        "--address",
        default=None,
        help="Wallet address to probe. Overrides SODEX_VERIFY_ADDRESS env "
        "and the cached .address file.",
    )
    parser.add_argument(
        "--save-address",
        action="store_true",
        help="Persist the resolved address to scripts/sodex_verify/.address "
        "(gitignored) so future runs need no env or flag.",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout (s).")
    args = parser.parse_args()

    address = _resolve_address(args.address)
    if not address:
        print(
            "ERROR: no address available. Either:\n"
            "  - pass --address 0x...\n"
            "  - export SODEX_VERIFY_ADDRESS=0x...\n"
            "  - or run once with --save-address to cache it.\n"
            "Run scripts/sodex_verify/gen_burner.py if you need a fresh burner.",
            file=sys.stderr,
        )
        return 2

    if args.save_address:
        _save_address(address)
        print(f"Cached address to {_ADDRESS_CACHE_PATH}", file=sys.stderr)

    return asyncio.run(main_async(args.out, address, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
