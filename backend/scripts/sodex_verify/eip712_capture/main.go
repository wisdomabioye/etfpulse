// V.1 — Capture canonical EIP-712 fixtures for SoDEX testnet.
//
// Operator-only. Run ONCE locally, commit the JSON output to
// tests/fixtures/sodex_eip712_golden.json. The Python backend then asserts
// byte-for-byte equality against this fixture in CI — production never runs Go.
//
// Why Go: the gateway re-marshals incoming request bodies via Go's
// json.Marshal to verify signatures. Struct field order is the canonical
// signing order. Reproducing this in Python is the brittle path; pinning
// fixtures from a Go capture is the safe path.
//
// What this program does NOT use: the sodex-go-sdk-public package itself,
// to avoid network/version coupling for a one-shot capture. Instead it
// replicates the documented algorithm (api.md §"Typed signature"):
//
//   1. Serialize {"type":<actionName>,"params":<request>} as compact JSON
//      with Go-struct field order.
//   2. payloadHash = keccak256(compactJSON)
//   3. EIP-712 typed-data hash over ExchangeAction{payloadHash, nonce}
//      with domain {name, version:"1", chainId, verifyingContract:0x0}.
//   4. ECDSA-sign the typed-data hash, prepend byte 0x01.
//
// Run:
//
//	export SODEX_VERIFY_PRIVKEY=0x...   # from gen_burner.py
//	export SODEX_VERIFY_ADDRESS=0x...   # ditto, used for assertion only
//	go run . > ../../tests/fixtures/sodex_eip712_golden.json
//
// The fixture commits the burner address + signatures intentionally — the
// key is throwaway and signatures over fixed nonces are deterministic.

package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"os"
	"strings"

	"github.com/ethereum/go-ethereum/common"
	ethmath "github.com/ethereum/go-ethereum/common/math"
	"github.com/ethereum/go-ethereum/common/hexutil"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/signer/core/apitypes"
)

// ---------------------------------------------------------------------------
// Request types — field order MUST match sodex-go-sdk-public structs exactly.
// Comments cite the schema and api.md rules.
// ---------------------------------------------------------------------------

// Spot — BatchNewOrderItem (order is the api.md / schema.md order).
type spotNewOrderItem struct {
	SymbolID    uint64  `json:"symbolID"`
	ClOrdID     string  `json:"clOrdID"`
	Side        int     `json:"side"`        // OrderSideEnum: 1=BUY, 2=SELL
	Type        int     `json:"type"`        // OrderTypeEnum: 1=LIMIT, 2=MARKET
	TimeInForce int     `json:"timeInForce"` // 1=GTC, 3=IOC, 4=GTX
	Price       *string `json:"price,omitempty"`
	Quantity    *string `json:"quantity,omitempty"`
	Funds       *string `json:"funds,omitempty"`
}

type spotBatchNewOrderRequest struct {
	AccountID uint64             `json:"accountID"`
	Orders    []spotNewOrderItem `json:"orders"`
}

// Spot — BatchCancelOrderItem.
type spotCancelItem struct {
	SymbolID    uint64  `json:"symbolID"`
	ClOrdID     string  `json:"clOrdID"`
	OrderID     *uint64 `json:"orderID,omitempty"`
	OrigClOrdID *string `json:"origClOrdID,omitempty"`
}

type spotBatchCancelOrderRequest struct {
	AccountID uint64           `json:"accountID"`
	Cancels   []spotCancelItem `json:"cancels"`
}

// Perps — PerpsOrderItem (clOrdID, modifier, side, type, timeInForce, price,
// quantity, funds, stopPrice, stopType, triggerType, reduceOnly, positionSide).
// Exact order verbatim from api.md §"Important rules" #2.
type perpsOrderItem struct {
	ClOrdID      string  `json:"clOrdID"`
	Modifier     int     `json:"modifier"`     // OrderModifier: 1=NORMAL, 2=STOP, 3=BRACKET, 4=ATTACHED_STOP
	Side         int     `json:"side"`         // 1=BUY, 2=SELL
	Type         int     `json:"type"`         // 1=LIMIT, 2=MARKET
	TimeInForce  int     `json:"timeInForce"`  // 1=GTC, 3=IOC, 4=GTX
	Price        *string `json:"price,omitempty"`
	Quantity     *string `json:"quantity,omitempty"`
	Funds        *string `json:"funds,omitempty"`
	StopPrice    *string `json:"stopPrice,omitempty"`
	StopType     *int    `json:"stopType,omitempty"`
	TriggerType  *int    `json:"triggerType,omitempty"`
	ReduceOnly   bool    `json:"reduceOnly"`
	PositionSide int     `json:"positionSide"` // PositionSide: 1=BOTH (only one supported today)
}

type perpsNewOrderRequest struct {
	AccountID uint64           `json:"accountID"`
	SymbolID  uint64           `json:"symbolID"`
	Orders    []perpsOrderItem `json:"orders"`
}

// ---------------------------------------------------------------------------
// Signing primitives.
// ---------------------------------------------------------------------------

// computePayloadHash returns keccak256(compactJSON({type, params})) as 0x-hex.
func computePayloadHash(actionType string, params any) (string, string, error) {
	// json.Marshal is compact + struct-field-order by default. This is the
	// exact rule the gateway uses to re-verify (api.md §"Important rules" #1+#2).
	paramsBytes, err := json.Marshal(params)
	if err != nil {
		return "", "", fmt.Errorf("marshal params: %w", err)
	}
	// Concatenate by hand to guarantee {"type":...,"params":...} order; a
	// struct with json tags would also work but the spec is explicit that
	// "type" comes first and "params" is the raw embedded object.
	payload := []byte(`{"type":"` + actionType + `","params":`)
	payload = append(payload, paramsBytes...)
	payload = append(payload, '}')
	hash := crypto.Keccak256(payload)
	return string(payload), "0x" + hex.EncodeToString(hash), nil
}

// signTypedData builds the EIP-712 ExchangeAction message, hashes per EIP-712,
// signs, and prepends the SoDEX signature-type byte 0x01.
func signTypedData(
	domainName string,
	chainID int64,
	payloadHashHex string,
	nonce uint64,
	privKeyHex string,
) (apitypes.TypedData, string, string, error) {
	typedData := apitypes.TypedData{
		Types: apitypes.Types{
			"EIP712Domain": {
				{Name: "name", Type: "string"},
				{Name: "version", Type: "string"},
				{Name: "chainId", Type: "uint256"},
				{Name: "verifyingContract", Type: "address"},
			},
			"ExchangeAction": {
				{Name: "payloadHash", Type: "bytes32"},
				{Name: "nonce", Type: "uint64"},
			},
		},
		PrimaryType: "ExchangeAction",
		Domain: apitypes.TypedDataDomain{
			Name:              domainName,
			Version:           "1",
			ChainId:           hexOrDecimalFromInt(chainID),
			VerifyingContract: "0x0000000000000000000000000000000000000000",
		},
		Message: apitypes.TypedDataMessage{
			"payloadHash": payloadHashHex,
			"nonce":       fmt.Sprintf("%d", nonce),
		},
	}

	domainSep, err := typedData.HashStruct("EIP712Domain", typedData.Domain.Map())
	if err != nil {
		return typedData, "", "", fmt.Errorf("hash domain: %w", err)
	}
	msgHash, err := typedData.HashStruct(typedData.PrimaryType, typedData.Message)
	if err != nil {
		return typedData, "", "", fmt.Errorf("hash message: %w", err)
	}
	rawData := []byte{0x19, 0x01}
	rawData = append(rawData, domainSep...)
	rawData = append(rawData, msgHash...)
	digest := crypto.Keccak256(rawData)

	pkBytes, err := hexutil.Decode(privKeyHex)
	if err != nil {
		return typedData, "", "", fmt.Errorf("decode privkey: %w", err)
	}
	pk, err := crypto.ToECDSA(pkBytes)
	if err != nil {
		return typedData, "", "", fmt.Errorf("parse privkey: %w", err)
	}
	sig, err := crypto.Sign(digest, pk)
	if err != nil {
		return typedData, "", "", fmt.Errorf("sign: %w", err)
	}
	// go-ethereum returns v in {0,1}; ETH convention adds 27. SoDEX expects
	// the raw 65 bytes with v ∈ {0,1} per EIP-712 (no 27 offset), then
	// prepends signature-type byte 0x01.
	// Empirically: the gateway accepts v as either {0,1} or {27,28}; we
	// normalize to {27,28} to match common wallet behaviour (and what the
	// SDK emits).
	if sig[64] < 27 {
		sig[64] += 27
	}
	rawSigHex := "0x" + hex.EncodeToString(sig)
	typedSigHex := "0x01" + hex.EncodeToString(sig)
	return typedData, rawSigHex, typedSigHex, nil
}

// hexOrDecimalFromInt is a small helper because apitypes.TypedDataDomain.ChainId
// is *math.HexOrDecimal256 — convert from an int64 via big.Int.
func hexOrDecimalFromInt(n int64) *ethmath.HexOrDecimal256 {
	h := ethmath.HexOrDecimal256(*big.NewInt(n))
	return &h
}

// ---------------------------------------------------------------------------
// Case definitions and main.
// ---------------------------------------------------------------------------

type fixtureCase struct {
	Name             string                 `json:"name"`
	Description      string                 `json:"description"`
	Venue            string                 `json:"venue"`             // "spot" | "perps"
	DomainName       string                 `json:"domain_name"`       // "spot" | "futures"
	ChainID          int64                  `json:"chain_id"`          // 286623 | 138565
	ActionType       string                 `json:"action_type"`       // "newOrder" | "cancelOrder"
	Nonce            uint64                 `json:"nonce"`
	Request          interface{}            `json:"request"`           // canonical struct (will marshal in order)
	PayloadJSON      string                 `json:"payload_json"`      // compact JSON of {type, params}
	PayloadHash      string                 `json:"payload_hash"`      // keccak256 of payload_json, 0x-hex
	EIP712Domain     map[string]interface{} `json:"eip712_domain"`
	EIP712Message    map[string]interface{} `json:"eip712_message"`
	BurnerAddress    string                 `json:"burner_address"`
	SignatureRaw     string                 `json:"signature_raw"`     // 65-byte 0x-hex (no type prefix)
	SignatureTyped   string                 `json:"signature_typed"`   // 0x01-prefixed
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	pk := strings.TrimSpace(os.Getenv("SODEX_VERIFY_PRIVKEY"))
	addr := strings.TrimSpace(os.Getenv("SODEX_VERIFY_ADDRESS"))
	if pk == "" || addr == "" {
		return fmt.Errorf("SODEX_VERIFY_PRIVKEY and SODEX_VERIFY_ADDRESS must be set " +
			"(run scripts/sodex_verify/gen_burner.py first)")
	}

	// Self-check: address derives from private key. Cheap sanity guard against
	// operator pasting mismatched env vars.
	pkBytes, err := hexutil.Decode(pk)
	if err != nil {
		return fmt.Errorf("decode privkey: %w", err)
	}
	priv, err := crypto.ToECDSA(pkBytes)
	if err != nil {
		return fmt.Errorf("parse privkey: %w", err)
	}
	derived := crypto.PubkeyToAddress(priv.PublicKey).Hex()
	if !strings.EqualFold(derived, addr) {
		return fmt.Errorf("address mismatch: env=%s derived=%s", addr, derived)
	}

	const testnetSpotChain int64 = 286623
	const testnetPerpsChain int64 = 138565

	// Helpers for pointer-to-string (DecimalString is *string when omitempty).
	s := func(v string) *string { return &v }

	// ----- Cases -----
	type caseDef struct {
		name        string
		description string
		venue       string
		domain      string
		chain       int64
		actionType  string
		nonce       uint64
		request     any
	}
	defs := []caseDef{
		{
			name:        "spot_limit_buy",
			description: "Spot LIMIT BUY GTC — price + quantity, no funds.",
			venue:       "spot", domain: "spot", chain: testnetSpotChain,
			actionType: "newOrder", nonce: 1700000000000,
			request: spotBatchNewOrderRequest{
				AccountID: 1001,
				Orders: []spotNewOrderItem{{
					SymbolID: 1, ClOrdID: "etfpulse-spot-1",
					Side: 1, Type: 1, TimeInForce: 1,
					Price: s("65000"), Quantity: s("0.01"),
				}},
			},
		},
		{
			name:        "spot_market_sell",
			description: "Spot MARKET SELL IOC — quantity only, no price, no funds.",
			venue:       "spot", domain: "spot", chain: testnetSpotChain,
			actionType: "newOrder", nonce: 1700000000001,
			request: spotBatchNewOrderRequest{
				AccountID: 1001,
				Orders: []spotNewOrderItem{{
					SymbolID: 1, ClOrdID: "etfpulse-spot-2",
					Side: 2, Type: 2, TimeInForce: 3,
					Quantity: s("0.005"),
				}},
			},
		},
		{
			name:        "spot_market_buy_funds",
			description: "Spot MARKET BUY IOC — funds only (covers market-buy path that uses funds, not quantity).",
			venue:       "spot", domain: "spot", chain: testnetSpotChain,
			actionType: "newOrder", nonce: 1700000000002,
			request: spotBatchNewOrderRequest{
				AccountID: 1001,
				Orders: []spotNewOrderItem{{
					SymbolID: 1, ClOrdID: "etfpulse-spot-3",
					Side: 1, Type: 2, TimeInForce: 3,
					Funds: s("100"),
				},
				},
			},
		},
		{
			name:        "spot_cancel_by_clordid",
			description: "Spot batch cancel by origClOrdID (orderID omitted).",
			venue:       "spot", domain: "spot", chain: testnetSpotChain,
			actionType: "cancelOrder", nonce: 1700000000003,
			request: spotBatchCancelOrderRequest{
				AccountID: 1001,
				Cancels: []spotCancelItem{{
					SymbolID: 1, ClOrdID: "etfpulse-cancel-1",
					OrigClOrdID: s("etfpulse-spot-1"),
				}},
			},
		},
		{
			name:        "perps_limit_buy",
			description: "Perps LIMIT BUY GTC NORMAL — price + quantity, reduceOnly=false, positionSide=BOTH.",
			venue:       "perps", domain: "futures", chain: testnetPerpsChain,
			actionType: "newOrder", nonce: 1700000000004,
			request: perpsNewOrderRequest{
				AccountID: 1001, SymbolID: 1,
				Orders: []perpsOrderItem{{
					ClOrdID: "etfpulse-perps-1",
					Modifier: 1, Side: 1, Type: 1, TimeInForce: 1,
					Price: s("65000"), Quantity: s("0.01"),
					ReduceOnly: false, PositionSide: 1,
				}},
			},
		},
		{
			name:        "perps_market_sell_reduce_only",
			description: "Perps MARKET SELL IOC NORMAL reduceOnly=true — closes a long.",
			venue:       "perps", domain: "futures", chain: testnetPerpsChain,
			actionType: "newOrder", nonce: 1700000000005,
			request: perpsNewOrderRequest{
				AccountID: 1001, SymbolID: 1,
				Orders: []perpsOrderItem{{
					ClOrdID: "etfpulse-perps-2",
					Modifier: 1, Side: 2, Type: 2, TimeInForce: 3,
					Quantity: s("0.01"),
					ReduceOnly: true, PositionSide: 1,
				}},
			},
		},
	}

	cases := make([]fixtureCase, 0, len(defs))
	for _, d := range defs {
		payloadJSON, payloadHashHex, err := computePayloadHash(d.actionType, d.request)
		if err != nil {
			return fmt.Errorf("case %s: %w", d.name, err)
		}
		typedData, rawSig, typedSig, err := signTypedData(
			d.domain, d.chain, payloadHashHex, d.nonce, pk,
		)
		if err != nil {
			return fmt.Errorf("case %s: %w", d.name, err)
		}
		cases = append(cases, fixtureCase{
			Name:           d.name,
			Description:    d.description,
			Venue:          d.venue,
			DomainName:     d.domain,
			ChainID:        d.chain,
			ActionType:     d.actionType,
			Nonce:          d.nonce,
			Request:        d.request,
			PayloadJSON:    payloadJSON,
			PayloadHash:    payloadHashHex,
			EIP712Domain:   typedData.Domain.Map(),
			EIP712Message:  typedData.Message,
			BurnerAddress:  common.HexToAddress(addr).Hex(),
			SignatureRaw:   rawSig,
			SignatureTyped: typedSig,
		})
	}

	out, err := json.MarshalIndent(map[string]any{
		"schema_version": "v1",
		"generated_by":   "scripts/sodex_verify/eip712_capture/main.go",
		"cases":          cases,
	}, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal output: %w", err)
	}
	_, err = os.Stdout.Write(append(out, '\n'))
	return err
}
