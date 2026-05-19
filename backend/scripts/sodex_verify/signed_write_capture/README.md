# V.3 — Signed-write fixture capture (Go, operator-only)

**Captures real testnet response shapes for SoDEX signed writes** —
the byte-exact contract D.2's HTTP adapter tests verify against.
Mirrors V.1's structure (signing primitives + request structs are
copied verbatim), but adds HTTP submission against the live testnet
gateway and captures the response envelope.

## Why V.3 vs V.1

V.1 (`eip712_capture/`) captures **local signing** — what bytes does
the Go SDK produce for a given request, what's the `payloadHash`,
what's the wallet signature. No network calls.

V.3 (this directory) captures **gateway acceptance** — does the live
gateway accept our signed request, what does the success response
JSON look like, what does an error response look like, are headers
case-sensitive, is chainId actually per-environment or per-venue.

V.1 + V.3 together pin the entire request/response wire contract.

## Prerequisites (operator setup)

1. **Burner wallet** — generated via V.0:

   ```bash
   uv run python scripts/sodex_verify/gen_burner.py
   ```

   The burner file lives at `~/.sodex_verify/burner.json`. V.3 reads
   it automatically.

2. **Fund the burner** with testnet vUSDC. Path varies — either
   the SoDEX testnet faucet (if available) or a testnet EVM bridge
   from a chain where you have funds. We need enough USDC to cover
   the test orders' notional ($10 spot + $10 perps = $20 minimum
   with some buffer for fees + slippage; $100 is safe).

3. **Register the burner as an API key on its own account.** Use
   the SoDEX testnet frontend's "API Keys" page — connect the
   burner wallet, click "Add API key", confirm. **The registered
   key gets a NAME** (defaults to `"default"`). That name is what
   gets passed in the `X-API-Key` HTTP header on every signed
   write — NOT the EVM address. See "API key model" below.

4. **Verify registration:**

   ```bash
   curl -s "https://testnet-gw.sodex.dev/api/v1/spot/accounts/<BURNER_ADDR>/api-keys" | jq
   ```

   Expected: at least one entry where `publicKey` equals the burner
   address (lowercase). Note its `name` — that's the value you'll
   set in `SODEX_API_KEY_NAME` if it isn't the default `"default"`.

## API key model (load-bearing — got this wrong on first capture)

The local docs snapshot at `docs/sodex/.../api.md` says "EVM address
as the API key" — that wording misled me on the first capture. The
authoritative docs at https://sodex.com/documentation/api/api are
explicit:

> Passed in the `X-API-Key` HTTP header (despite the header's name,
> the value is the **key NAME, not a public key or private key**).

The flow at request time:
1. Gateway reads `X-API-Key: <name>` and looks up the named key on
   the target accountID (taken from the request body's `accountID`).
2. Recovers the signer address from `X-API-Sign` (typed signature).
3. Verifies the recovered address equals the named key's `publicKey`.

If the name in `X-API-Key` doesn't match a registered key on that
account, gateway returns `{code: -1, error: "API key not found"}`
— even if the signature itself is valid. (We hit this exact case
on the V.3 first run by sending the EVM address as the X-API-Key
value; recovered signer matched the registered publicKey just fine
but the name lookup failed.)

V.3 defaults `apiKeyName = "default"`. Override via env var if your
frontend registered the burner under a different name:

```bash
SODEX_API_KEY_NAME=mykey go run .
```

## What V.3 captures (6 probes)

| # | Name | Method | Endpoint | Notes |
|---|------|--------|----------|-------|
| 1 | `spot_account_state` | GET | `/spot/accounts/{addr}/state` | Read aid for use in writes |
| 2 | `spot_batch_new` | POST | `/spot/trade/orders/batch` | LIMIT BUY @ $100 (below market) |
| 3 | `spot_batch_cancel` | DELETE | `/spot/trade/orders/batch` | Cancels #2 by `origClOrdID` |
| 4 | `perps_account_state` | GET | `/perps/accounts/{addr}/state` | Read perps aid (distinct from spot) |
| 5 | `perps_batch_new` | POST | `/perps/trade/orders` | LIMIT BUY @ $100 (below market) |
| 6 | `perps_batch_cancel` | DELETE | `/perps/trade/orders` | Cancels #5 by `clOrdID` |

**Safety**: every test order is a LIMIT BUY at $100 against vETH /
ETH-USD (id=2 on both venues; market ~$3500+). The notional is just
above the symbol's `minNotional` ($5 spot / $10 perps). The order
will NOT fill. The matching cancel fires immediately after the new-
order probe.

Worst-case if a cancel fails: the order sits in the book until the
burner's balance is too low to support it. Operator can manually
cancel via the SoDEX testnet frontend.

## Run

From `backend/scripts/sodex_verify/signed_write_capture/`:

```bash
go run . > ../../../tests/fixtures/sodex_signed_write_responses.json
```

The program:
- Reads the burner from `~/.sodex_verify/burner.json`.
  Override the file LOCATION (not credentials) via `SODEX_BURNER_PATH`.
  The legacy `SODEX_VERIFY_ADDRESS`/`SODEX_VERIFY_PRIVKEY` env vars
  are explicitly **ignored** (with a stderr warning) to prevent the
  stale-credentials footgun where an old shell export silently
  shadows a regenerated burner.
- Resolves the API key NAME from `SODEX_API_KEY_NAME` env var,
  defaulting to `"default"` if unset. Sent in the `X-API-Key`
  header on every signed write.
- Self-checks that the address derives from the private key.
- Runs the 6 probes sequentially.
- Emits a JSON fixture to stdout, scrubbing the burner address to
  `<BURNER_ADDR>` placeholder.

On any probe failure, the program writes a partial fixture (all
probes captured so far) and exits non-zero — debugging-friendly.

## Fixture shape

```json
{
  "schema_version": "v1",
  "generated_by":   "scripts/sodex_verify/signed_write_capture/main.go",
  "burner_address_placeholder": "<BURNER_ADDR>",
  "probes": [
    {
      "name": "spot_batch_new",
      "description": "POST /spot/trade/orders/batch — ...",
      "method": "POST",
      "url": "https://testnet-gw.sodex.dev/api/v1/spot/trade/orders/batch",
      "headers_sent": {
        "X-API-Key":   "<BURNER_ADDR>",
        "X-API-Sign":  "0x01...",
        "X-API-Nonce": "1779139281482"
      },
      "request_body": "{\"accountID\":..., \"orders\":[...]}",
      "payload_json": "{\"type\":\"newOrder\",\"params\":{...}}",
      "payload_hash": "0x...",
      "nonce":        1779139281482,
      "domain_name":  "spot",
      "chain_id":     138565,
      "status":       200,
      "elapsed_ms":   523,
      "response_content_type": "application/json; charset=utf-8",
      "response": { ... }
    },
    ...
  ]
}
```

## What V.3 does NOT do

- **Never write the private key to logs or stdout.** Address is
  printed in errors only.
- **Never submit to mainnet** — testnet base URLs hardcoded.
- **Never get imported by `etfpulse/`** — same boundary as V.1.
- **Never run automatically** — operator-triggered only.

## When to re-run

- Before D.2 lands (initial capture).
- If SoDEX bumps the gateway version and response shape changes
  (D.5 live smoke would catch this; V.3 re-capture fixes the
  offline fixture).
- If the burner is regenerated via `gen_burner.py --force` (old
  aid is now stale).

## Anti-drift

This directory shares V.1's anti-drift posture: no `etfpulse/*`
module may import from here. D.1's `TestAntiDriftRule27` grep test
catches signing-primitive imports inside `etfpulse/adapters/sodex/`;
combined with code review on `scripts/sodex_verify/` cross-imports,
the boundary is preserved.
