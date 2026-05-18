# V.1 — EIP-712 fixture capture (Go, operator-only)

**One-shot capture.** This Go program produces
`tests/fixtures/sodex_eip712_golden.json`. The Python backend (D.1 EIP-712
builders) asserts byte-for-byte equality against that fixture in CI. CI
never runs Go; production never runs Go. This program runs **once**, locally,
on operator hardware, to capture the canonical signing shape.

## Why Go, not Python or viem

The SoDEX gateway re-marshals incoming request bodies via Go's `json.Marshal`
and re-derives `payloadHash` from the result (see `docs/api.md` §"Important
rules for producing a correct `payloadHash`"). Struct field order, `omitempty`
elision, and `DecimalString`-as-string are all enforced by the gateway's Go
type system. Reproducing the *gateway's* serializer in Python is the brittle
path. Pinning the bytes from a Go capture is the safe path — the Python
builder's only job is then to emit identical bytes.

The program does **not** depend on `sodex-go-sdk-public` directly. It
replicates the algorithm from `api.md` (compact JSON → keccak256 →
EIP-712 typed-data → ECDSA → prepend `0x01`). Avoids version pinning a
single-purpose external dep for a one-shot capture; the SDK's struct
order is mirrored in `main.go` with inline citations to the schema.

## Cases captured (6)

| Name | Venue | Action | Coverage |
|---|---|---|---|
| `spot_limit_buy` | spot | newOrder | LIMIT + price + quantity, omits `funds` |
| `spot_market_sell` | spot | newOrder | MARKET SELL IOC, omits `price` and `funds` |
| `spot_market_buy_funds` | spot | newOrder | MARKET BUY IOC with `funds`, omits `price`+`quantity` |
| `spot_cancel_by_clordid` | spot | cancelOrder | Batch cancel, omits `orderID` |
| `perps_limit_buy` | perps | newOrder | NORMAL + GTC, `reduceOnly=false`, `positionSide=BOTH` |
| `perps_market_sell_reduce_only` | perps | newOrder | MARKET IOC, `reduceOnly=true`, closes long |

Six cases hit every documented permutation of optional/required +
`omitempty` + `DecimalString` we expect to exercise in D.1. Extend by
adding to `defs` in `main.go` and re-running.

## Prerequisites

- Go 1.21+ (`go version`)
- A throwaway burner key from `gen_burner.py` (V.0).
- Network access to fetch the `go-ethereum` dep (~50 MB) — first run only.

## Run

From `backend/`:

```bash
# 1. Burner (V.0)
uv run python scripts/sodex_verify/gen_burner.py
# copy the printed export lines into your shell
export SODEX_VERIFY_ADDRESS=0x...
export SODEX_VERIFY_PRIVKEY=0x...

# 2. Capture
cd scripts/sodex_verify/eip712_capture
go mod tidy        # first run only; pulls go-ethereum
go run . > ../../../tests/fixtures/sodex_eip712_golden.json

# 3. Verify the fixture is well-formed JSON
python -m json.tool < ../../../tests/fixtures/sodex_eip712_golden.json > /dev/null && echo "ok"

# 4. Commit
cd ../../../..
git add tests/fixtures/sodex_eip712_golden.json
```

## Determinism

Nonces are fixed (`1700000000000..1700000000005`), so given the same burner
key, repeat runs produce **byte-identical** output. Re-captures only happen if:

- The action types or struct fields change upstream (SDK bump). Update
  `main.go` field order, re-run, commit the new fixture, update the Python
  builder in D.1, and run `pytest tests/test_pipeline/test_sodex_eip712.py`
  to confirm equality.
- A new case is added to broaden coverage.

## Committing the burner key

The burner address + signatures are in the committed fixture **on purpose**:
- The address makes the EIP-712 self-checkable (verify-signature in Python
  reproduces it).
- The key itself is **NOT** in the fixture — only signatures over fixed
  nonces, which are not reusable as anything but exact-replay traffic.
- The burner has no real-money authority (testnet only, `chainId` 286623
  for spot / 138565 for perps — never mainnet).

If you re-run with a different burner address, the fixture's signatures
change but the `payload_json` / `payload_hash` / `eip712_message` fields
stay identical. Python tests assert against the latter three, not the
signature itself, for exactly this reason.

## What this program never does

- Never submits a request to SoDEX (signing only — V.2 does the HTTP probe).
- Never writes the private key anywhere (reads from env, signs in-memory).
- Never imports from `etfpulse/` — this directory is one-way (see parent
  `README.md` for the boundary rule).
