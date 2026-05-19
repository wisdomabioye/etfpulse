# `scripts/sodex_verify/` — SoDEX verification tooling (operator-only)

**One-time (and periodic re-capture) verification scripts that produce
canonical SoDEX request/response fixtures BEFORE the production execution
surface (Stage 09: C.6 + D.1–D.5) is built and certified. These scripts are
NEVER imported by production backend code.**

## Boundary rule (non-negotiable)

ETFPulse's production backend never holds, generates, signs with, or
stores private keys. SoDEX execution flows go through wallet-side
signing only (wagmi/viem + WalletConnect in the frontend).

This directory is an exception **for verification only**:

- `gen_burner.py` generates a throwaway burner key and persists it to
  `~/.sodex_verify/burner.json` (OUTSIDE the repo — cannot leak into
  git history even if `.gitignore` is misconfigured).
- The Go capture programs (V.1 EIP-712, V.3 signed-write) read the
  burner from that file and sign locally.
- After the SoDEX execution surface is stable, the operator deletes
  the burner file (`rm ~/.sodex_verify/burner.json`).
- **No production code path imports from this directory.** Enforced
  by code review and the D.1 anti-drift rule 27 grep test.

## Scripts

| Script | Stage | Purpose |
|---|---|---|
| `gen_burner.py` | V.0 | Generate a persistent throwaway burner wallet at `~/.sodex_verify/burner.json` (chmod 600). Refuses overwrite without `--force`. `--print` reads the existing file. |
| `eip712_capture/` (Go) | V.1 | Capture canonical EIP-712 signing fixtures → `tests/fixtures/sodex_eip712_golden.json`. D.1 builders verify byte-exact against this in CI. |
| `endpoint_probe.py` | V.2 | Probe REST read endpoints (10 spot + 10 perps) → `tests/fixtures/sodex_endpoint_responses.json`. No signing, no order placement. |
| `signed_write_capture/` (Go) | V.3 | (planned) Capture signed-write response shapes. Requires funded + registered burner. |

## Burner file (V.0)

Default path: `~/.sodex_verify/burner.json` (override via `SODEX_BURNER_PATH`).
File mode 0600, parent directory 0700. Schema:

```json
{
  "schema_version": 1,
  "network": "testnet",
  "address": "0x...",
  "private_key": "0x...",
  "created_at": "2026-05-19T..."
}
```

Lifecycle:

```bash
# 1. First-time setup — generates and persists.
uv run python scripts/sodex_verify/gen_burner.py

# 2. Later, re-print without regenerating (e.g. before V.3).
uv run python scripts/sodex_verify/gen_burner.py --print

# 3. Override location for one run (e.g. CI snapshot).
SODEX_BURNER_PATH=/tmp/burner.json uv run python scripts/sodex_verify/gen_burner.py

# 4. Regenerate (CURRENT KEY IS LOST — and any testnet funds on it).
uv run python scripts/sodex_verify/gen_burner.py --force

# 5. When all verification is complete, destroy the burner.
rm ~/.sodex_verify/burner.json
```

## Capture flow (V.1 + V.2)

```bash
# Generate burner (one-time).
uv run python scripts/sodex_verify/gen_burner.py

# V.1 — EIP-712 fixture capture (one-time, requires Go installed locally).
# Reads the burner from ~/.sodex_verify/burner.json via env vars.
export SODEX_VERIFY_ADDRESS="$(jq -r .address ~/.sodex_verify/burner.json)"
export SODEX_VERIFY_PRIVKEY="$(jq -r .private_key ~/.sodex_verify/burner.json)"
cd scripts/sodex_verify/eip712_capture && go run . > ../../../tests/fixtures/sodex_eip712_golden.json
git add tests/fixtures/sodex_eip712_golden.json && git commit ...

# V.2 — REST endpoint shape probe (read-only; no funding needed).
uv run python scripts/sodex_verify/endpoint_probe.py \
    --out tests/fixtures/sodex_endpoint_responses.json
```

## Capture flow (V.3 — signed writes)

V.3 requires operator setup beyond V.0/V.1/V.2:

1. Generate burner via V.0 (or reuse existing).
2. **Fund** the burner address with testnet vUSDC via the SoDEX testnet faucet
   or testnet bridge (operator step, off-tooling).
3. **Register the burner as an API key on its own account** via the
   SoDEX testnet frontend's "API Keys" page (connect burner wallet,
   "Add API key"). The registered key gets a NAME (defaults to
   `"default"`) — that name is what V.3 sends in the `X-API-Key`
   header, NOT the EVM address. The local doc snapshot's "EVM address
   as the API key" wording is misleading; the authoritative docs at
   https://sodex.com/documentation/api/api clarify the header value
   is the key name. See `signed_write_capture/README.md` for the
   full request-time flow.
4. **Unset any legacy V.1 env vars** before running V.3 — V.1 used
   `SODEX_VERIFY_ADDRESS`/`SODEX_VERIFY_PRIVKEY`; V.3 reads the
   persistent burner file directly and explicitly IGNORES those env
   vars (with a stderr warning) to avoid stale-credentials drift.

   ```bash
   unset SODEX_VERIFY_ADDRESS SODEX_VERIFY_PRIVKEY
   ```

5. **Set the API key name** if your frontend registered under a name
   other than `"default"`:

   ```bash
   # Optional — only if your key isn't named "default"
   export SODEX_API_KEY_NAME=mykey
   ```

6. Run `signed_write_capture/`:

   ```bash
   cd scripts/sodex_verify/signed_write_capture
   go run . > ../../../tests/fixtures/sodex_signed_write_responses.json
   ```

7. Commit `tests/fixtures/sodex_signed_write_responses.json`.

## What these scripts never do

- Never write a private key to a logged location (stdout printing is
  the operator-visible exception; no logs, no DB, no `.env*`).
- Never persist anything to the production DB.
- Never submit to mainnet — `chainId` pinned to testnet (`138565`).
- Never get imported by any module under `etfpulse/`.

## Anti-drift

If anyone proposes a code path under `etfpulse/` that imports from
`scripts.sodex_verify`, reject in code review. The boundary is the
whole point. D.1's anti-drift rule 27 grep test catches forbidden
signing-primitive imports inside `etfpulse/adapters/sodex/`; the
verify tooling is intentionally excluded from that scope but is also
verified by review never to be cross-imported.
