// V.3 — Capture canonical SoDEX signed-write request/response shapes
// against the live testnet.
//
// Operator-only. Run when a funded + API-key-registered burner is
// available. Produces tests/fixtures/sodex_signed_write_responses.json
// — the byte-exact contract D.2's write-path tests verify against.
//
// What V.3 adds over V.1
// ----------------------
// V.1 captures local signing only — the bytes the wallet hashes and
// signs. V.3 captures the gateway acceptance + response — what success
// JSON the gateway returns, what error envelope it returns on rejects,
// what header conventions it enforces, and whether chainId is per-env
// or per-venue (the D.1 honesty gap).
//
// Probes (5 total)
// ----------------
//   1. spot_account_state   — GET /spot/accounts/{addr}/state → discover aid
//   2. spot_batch_new       — POST /spot/trade/orders/batch  (limit buy far below market, won't fill)
//   3. spot_batch_cancel    — DELETE /spot/trade/orders/batch (cancels #2 by clOrdID)
//   4. perps_batch_new      — POST /perps/trade/orders        (same shape, perps)
//   5. perps_batch_cancel   — DELETE /perps/trade/orders      (cancels #4)
//
// All orders use LIMIT BUY at a price well below market with notional
// just above the symbol's minNotional. They sit in the book until the
// matching cancel fires (immediately, in the next probe). Worst case
// if the cancel fails: the order remains in the book until the
// burner's testnet vUSDC balance is too small for it (limits are
// tiny — vETH $10 spot, ETH-USD $10 perps).
//
// Burner key — read from ~/.sodex_verify/burner.json (the persistent
// burner file written by gen_burner.py). Override the LOCATION via
// SODEX_BURNER_PATH. The legacy V.0/V.1/V.2 env vars
// (SODEX_VERIFY_ADDRESS + SODEX_VERIFY_PRIVKEY) are intentionally
// NOT consulted — they create a stale-credentials footgun when
// operators forget to unset them across burner regenerations.
//
// API key name — X-API-Key header carries the NAME of the registered
// key (e.g. "default"), NOT the EVM address. The web docs at
// https://sodex.com/documentation/api/api are explicit:
//   > "Passed in the X-API-Key HTTP header (despite the header's
//   >  name, the value is the key NAME, not a public key or private
//   >  key)"
// Defaults to "default" (the name auto-assigned by SoDEX to a master
// account's first self-registered key). Override via SODEX_API_KEY_NAME
// if the operator registered the burner under a different name.
//
// Run
// ---
//
//	cd scripts/sodex_verify/signed_write_capture
//	go run . > ../../../tests/fixtures/sodex_signed_write_responses.json
//
// The fixture commits the burner address — it's a throwaway. Signatures
// are committed (deterministic over the captured nonce + payload).
// Account ID + balances are NOT scrubbed beyond replacing the burner
// address with `<BURNER_ADDR>` in URLs and headers (the aid value
// itself is non-sensitive — it's a per-deployment auto-increment that
// changes if SoDEX resets testnet).
//
// Safety guards
// -------------
//   - HTTP client has a 10-second timeout per request.
//   - All test orders are LIMIT BUY at $100 (vETH/ETH ~$3500+, won't fill).
//   - The cancel probe fires IMMEDIATELY after the matching new-order.
//   - If account state shows aid=0, we error out (burner isn't registered).
//   - If account state's USDC balance is below test notional, we error out.
//
// Anti-drift
// ----------
// This program is NEVER imported by any module under etfpulse/. Same
// boundary as V.1 (eip712_capture/). The carve-out exists precisely
// because verification needs a throwaway signing key; production must
// not.

package main

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	ethmath "github.com/ethereum/go-ethereum/common/math"
	"github.com/ethereum/go-ethereum/common/hexutil"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/signer/core/apitypes"
)

// ---------------------------------------------------------------------------
// Constants — testnet endpoints + safety-tuned order parameters.
// ---------------------------------------------------------------------------

const (
	testnetSpotBase  = "https://testnet-gw.sodex.dev/api/v1/spot"
	testnetPerpsBase = "https://testnet-gw.sodex.dev/api/v1/perps"

	// Per api.md §"Typed signature": single chainId per environment.
	// (V.1 fixture captured 286623 spot + 138565 perps; V.3 verifies
	// which is right against the live gateway — D.1's honesty gap.)
	testnetChainID int64 = 138565

	// Symbol choices — vETH/ETH on both venues, id=2. minPrice 0.1,
	// stepSize 0.0001, minQty 0.0001. minNotional 5 (spot) / 10 (perps).
	// Limit buy at $100, qty 0.1 → notional $10. Below market (~$3500+)
	// so the order will NOT fill.
	testSpotSymbolID  uint64 = 2
	testPerpsSymbolID uint64 = 2
	testLimitPrice           = "100"
	testQuantity             = "0.1"

	httpTimeout = 10 * time.Second
)

// ---------------------------------------------------------------------------
// Request types — identical to V.1's structs. Field order MUST match
// the Go SDK / api.md §"Important rules" #2 exactly. Copied (not
// imported) because each capture program is standalone — the JSON
// serialization is the contract, not the Go type.
// ---------------------------------------------------------------------------

type spotNewOrderItem struct {
	SymbolID    uint64  `json:"symbolID"`
	ClOrdID     string  `json:"clOrdID"`
	Side        int     `json:"side"`
	Type        int     `json:"type"`
	TimeInForce int     `json:"timeInForce"`
	Price       *string `json:"price,omitempty"`
	Quantity    *string `json:"quantity,omitempty"`
	Funds       *string `json:"funds,omitempty"`
}

type spotBatchNewOrderRequest struct {
	AccountID uint64             `json:"accountID"`
	Orders    []spotNewOrderItem `json:"orders"`
}

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

type perpsOrderItem struct {
	ClOrdID      string  `json:"clOrdID"`
	Modifier     int     `json:"modifier"`
	Side         int     `json:"side"`
	Type         int     `json:"type"`
	TimeInForce  int     `json:"timeInForce"`
	Price        *string `json:"price,omitempty"`
	Quantity     *string `json:"quantity,omitempty"`
	Funds        *string `json:"funds,omitempty"`
	StopPrice    *string `json:"stopPrice,omitempty"`
	StopType     *int    `json:"stopType,omitempty"`
	TriggerType  *int    `json:"triggerType,omitempty"`
	ReduceOnly   bool    `json:"reduceOnly"`
	PositionSide int     `json:"positionSide"`
}

type perpsNewOrderRequest struct {
	AccountID uint64           `json:"accountID"`
	SymbolID  uint64           `json:"symbolID"`
	Orders    []perpsOrderItem `json:"orders"`
}

type perpsCancelItem struct {
	SymbolID uint64  `json:"symbolID"`
	OrderID  *uint64 `json:"orderID,omitempty"`
	ClOrdID  *string `json:"clOrdID,omitempty"`
}

type perpsCancelOrderRequest struct {
	AccountID uint64            `json:"accountID"`
	Cancels   []perpsCancelItem `json:"cancels"`
}

// ---------------------------------------------------------------------------
// Burner reading — persistent file is the SINGLE source of truth.
//
// V.0/V.1/V.2 used the print-and-export model (SODEX_VERIFY_ADDRESS +
// SODEX_VERIFY_PRIVKEY env vars). V.3 introduces the persistent
// burner file; supporting BOTH paths invites a real bug class —
// operators leave stale env vars in their shell across burner
// regenerations, point V.3 at the wrong key, and don't notice until
// the gateway rejects every signature.
//
// V.3 therefore reads the file ONLY. If the legacy env vars are set,
// we WARN loudly to stderr but ignore them. To override the file
// LOCATION (not credentials), use SODEX_BURNER_PATH.
// ---------------------------------------------------------------------------

type burnerFile struct {
	SchemaVersion int    `json:"schema_version"`
	Network       string `json:"network"`
	Address       string `json:"address"`
	PrivateKey    string `json:"private_key"`
	CreatedAt     string `json:"created_at"`
}

func readBurner() (address, privKey string, err error) {
	// Defensive: detect stale V.0/V.1/V.2 env vars and warn. These were
	// the credentials path before V.3; ignoring them now (rather than
	// silently preferring them) prevents the stale-shell-credentials bug.
	if os.Getenv("SODEX_VERIFY_ADDRESS") != "" || os.Getenv("SODEX_VERIFY_PRIVKEY") != "" {
		fmt.Fprintln(os.Stderr,
			"WARNING: SODEX_VERIFY_ADDRESS/SODEX_VERIFY_PRIVKEY are set but IGNORED by V.3.")
		fmt.Fprintln(os.Stderr,
			"         V.3 reads the persistent burner file (~/.sodex_verify/burner.json).")
		fmt.Fprintln(os.Stderr,
			"         Unset them to silence this warning:")
		fmt.Fprintln(os.Stderr,
			"           unset SODEX_VERIFY_ADDRESS SODEX_VERIFY_PRIVKEY")
		fmt.Fprintln(os.Stderr,
			"         To override the file LOCATION, use SODEX_BURNER_PATH.")
	}

	burnerPath := os.Getenv("SODEX_BURNER_PATH")
	if burnerPath == "" {
		home, herr := os.UserHomeDir()
		if herr != nil {
			return "", "", fmt.Errorf("locate home dir: %w", herr)
		}
		burnerPath = filepath.Join(home, ".sodex_verify", "burner.json")
	}

	data, err := os.ReadFile(burnerPath)
	if err != nil {
		return "", "", fmt.Errorf("read burner file at %s: %w (run scripts/sodex_verify/gen_burner.py to create one)", burnerPath, err)
	}
	var b burnerFile
	if err := json.Unmarshal(data, &b); err != nil {
		return "", "", fmt.Errorf("parse burner file at %s: %w", burnerPath, err)
	}
	if b.SchemaVersion != 1 {
		return "", "", fmt.Errorf("burner file schema mismatch at %s: got %d, expected 1", burnerPath, b.SchemaVersion)
	}
	if b.Address == "" || b.PrivateKey == "" {
		return "", "", fmt.Errorf("burner file at %s missing address or private_key", burnerPath)
	}
	return b.Address, b.PrivateKey, nil
}

// ---------------------------------------------------------------------------
// Signing primitives (identical to V.1).
// ---------------------------------------------------------------------------

func computePayloadHash(actionType string, params any) (string, string, error) {
	paramsBytes, err := json.Marshal(params)
	if err != nil {
		return "", "", fmt.Errorf("marshal params: %w", err)
	}
	payload := []byte(`{"type":"` + actionType + `","params":`)
	payload = append(payload, paramsBytes...)
	payload = append(payload, '}')
	hash := crypto.Keccak256(payload)
	return string(payload), "0x" + hex.EncodeToString(hash), nil
}

func signTypedData(
	domainName string,
	chainID int64,
	payloadHashHex string,
	nonce uint64,
	privKeyHex string,
) (string, string, error) {
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
		return "", "", fmt.Errorf("hash domain: %w", err)
	}
	msgHash, err := typedData.HashStruct(typedData.PrimaryType, typedData.Message)
	if err != nil {
		return "", "", fmt.Errorf("hash message: %w", err)
	}
	rawData := []byte{0x19, 0x01}
	rawData = append(rawData, domainSep...)
	rawData = append(rawData, msgHash...)
	digest := crypto.Keccak256(rawData)

	pkBytes, err := hexutil.Decode(privKeyHex)
	if err != nil {
		return "", "", fmt.Errorf("decode privkey: %w", err)
	}
	pk, err := crypto.ToECDSA(pkBytes)
	if err != nil {
		return "", "", fmt.Errorf("parse privkey: %w", err)
	}
	sig, err := crypto.Sign(digest, pk)
	if err != nil {
		return "", "", fmt.Errorf("sign: %w", err)
	}
	// SoDEX gateway expects the raw secp256k1 recovery ID — v ∈ {0, 1}.
	// go-ethereum's crypto.Sign returns v in {0, 1} natively. The V.1
	// comment claimed "the gateway accepts v as either {0,1} or {27,28}"
	// but that was unverified — V.3 against the live testnet returned
	// "Failed to recover signer: Invalid recovery ID: bad recovery id"
	// when we offset v to {27, 28} (the older ETH convention). Leaving
	// v as {0, 1} is the correct path.
	//
	// This DOES NOT affect D.1's golden tests — those compare payload_json,
	// payload_hash, and typed_data structure, NOT the signature bytes.
	// The frontend (wagmi/viem) is responsible for emitting the signature
	// in the form the gateway accepts; viem's signTypedData returns v in
	// {27, 28} by default, so D.4 will need a post-sig normalization step
	// to subtract 27 before submitting (or use viem's `serializeSignature`
	// with the appropriate option). See D.4 implementation notes.
	rawSigHex := "0x" + hex.EncodeToString(sig)
	typedSigHex := "0x01" + hex.EncodeToString(sig)
	return rawSigHex, typedSigHex, nil
}

func hexOrDecimalFromInt(n int64) *ethmath.HexOrDecimal256 {
	h := ethmath.HexOrDecimal256(*big.NewInt(n))
	return &h
}

// ---------------------------------------------------------------------------
// HTTP probe + fixture types.
// ---------------------------------------------------------------------------

type probeRecord struct {
	Name        string            `json:"name"`
	Description string            `json:"description"`
	Method      string            `json:"method"`
	URL         string            `json:"url"`
	HeadersSent map[string]string `json:"headers_sent"`
	RequestBody string            `json:"request_body,omitempty"`  // HTTP body (params only)
	PayloadJSON string            `json:"payload_json,omitempty"`  // bytes hashed for signing ({type,params})
	PayloadHash string            `json:"payload_hash,omitempty"`  // for signed writes
	Nonce       uint64            `json:"nonce,omitempty"`         // for signed writes
	DomainName  string            `json:"domain_name,omitempty"`   // for signed writes
	ChainID     int64             `json:"chain_id,omitempty"`      // for signed writes
	Status      int               `json:"status"`
	ElapsedMs   int64             `json:"elapsed_ms"`
	ContentType string            `json:"response_content_type"`
	Response    json.RawMessage   `json:"response"`
}

var httpClient = &http.Client{Timeout: httpTimeout}

func doRequest(method, url string, headers map[string]string, body []byte) (*probeRecord, error) {
	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, url, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	start := time.Now()
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http %s %s: %w", method, url, err)
	}
	defer resp.Body.Close()
	elapsed := time.Since(start).Milliseconds()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	// We try to embed the response as a parsed JSON object so the
	// fixture is readable. If the body isn't JSON we keep it as a
	// string for debuggability.
	var raw json.RawMessage
	if json.Valid(respBody) {
		raw = respBody
	} else {
		raw, _ = json.Marshal(string(respBody))
	}

	// Echo sent headers minus sensitive values. The burner is throwaway
	// but we still redact for clarity — none of these need to leak.
	headersEcho := map[string]string{}
	for k, v := range headers {
		// X-API-Sign is the typed signature; deterministic for a given
		// (payload, nonce, key) so committing it is fine.
		headersEcho[k] = v
	}

	return &probeRecord{
		Method:      method,
		URL:         url,
		HeadersSent: headersEcho,
		Status:      resp.StatusCode,
		ElapsedMs:   elapsed,
		ContentType: resp.Header.Get("Content-Type"),
		Response:    raw,
	}, nil
}

// ---------------------------------------------------------------------------
// Probes
// ---------------------------------------------------------------------------

// fetchAccountState reads /accounts/{addr}/state on the given venue
// and returns the parsed aid. Errors if aid is 0 (burner not registered).
func fetchAccountState(venue, baseURL, addr string) (uint64, *probeRecord, error) {
	url := fmt.Sprintf("%s/accounts/%s/state", baseURL, addr)
	rec, err := doRequest(http.MethodGet, url, nil, nil)
	if err != nil {
		return 0, nil, err
	}
	rec.Name = venue + "_account_state"
	rec.Description = "GET /accounts/{addr}/state — used to discover the burner's master accountID (aid)."

	if rec.Status != http.StatusOK {
		return 0, rec, fmt.Errorf("%s /accounts/.../state returned status %d (body: %s)", venue, rec.Status, string(rec.Response))
	}

	var envelope struct {
		Code int `json:"code"`
		Data struct {
			Aid uint64 `json:"aid"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Response, &envelope); err != nil {
		return 0, rec, fmt.Errorf("parse %s state envelope: %w", venue, err)
	}
	if envelope.Code != 0 {
		return 0, rec, fmt.Errorf("%s state non-zero code: %d", venue, envelope.Code)
	}
	if envelope.Data.Aid == 0 {
		return 0, rec, fmt.Errorf("%s burner has aid=0 — wallet %s is not registered for %s. Fund + register via SoDEX testnet frontend before retrying", venue, addr, venue)
	}
	return envelope.Data.Aid, rec, nil
}

// submitSignedWrite computes payloadHash + EIP-712 signature, sends the
// HTTP request with auth headers, and captures the result.
func submitSignedWrite(
	name, description, method, url, actionType, domainName string,
	chainID int64,
	nonce uint64,
	params any,
	apiKeyName, privKey string,
) (*probeRecord, error) {
	payloadJSON, payloadHash, err := computePayloadHash(actionType, params)
	if err != nil {
		return nil, fmt.Errorf("%s: payload hash: %w", name, err)
	}
	_, typedSig, err := signTypedData(domainName, chainID, payloadHash, nonce, privKey)
	if err != nil {
		return nil, fmt.Errorf("%s: sign: %w", name, err)
	}

	// HTTP body is the params object only — no {type, params} wrapper
	// per api.md L111. params is the request struct; json.Marshal
	// produces the same bytes as the signing payload's inner.
	body, err := json.Marshal(params)
	if err != nil {
		return nil, fmt.Errorf("%s: marshal body: %w", name, err)
	}

	// X-API-Key is the registered key's NAME (e.g. "default"), NOT the
	// EVM address. The gateway looks up the key by name on the target
	// accountID, then recovers the signer from X-API-Sign and verifies
	// it matches the key's `publicKey`. See the comment block in run()
	// where apiKeyName is resolved.
	headers := map[string]string{
		"X-API-Key":   apiKeyName,
		"X-API-Sign":  typedSig,
		"X-API-Nonce": fmt.Sprintf("%d", nonce),
	}
	rec, err := doRequest(method, url, headers, body)
	if err != nil {
		return nil, err
	}
	rec.Name = name
	rec.Description = description
	rec.RequestBody = string(body)
	rec.PayloadJSON = payloadJSON
	rec.PayloadHash = payloadHash
	rec.Nonce = nonce
	rec.DomainName = domainName
	rec.ChainID = chainID
	return rec, nil
}

// ---------------------------------------------------------------------------
// main + run.
// ---------------------------------------------------------------------------

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	burnerAddr, privKey, err := readBurner()
	if err != nil {
		return err
	}
	// Self-check: address derives from private key. Cheap sanity guard.
	pkBytes, err := hexutil.Decode(privKey)
	if err != nil {
		return fmt.Errorf("decode privkey: %w", err)
	}
	priv, err := crypto.ToECDSA(pkBytes)
	if err != nil {
		return fmt.Errorf("parse privkey: %w", err)
	}
	derived := crypto.PubkeyToAddress(priv.PublicKey).Hex()
	if !strings.EqualFold(derived, burnerAddr) {
		return fmt.Errorf("burner address mismatch: file/env=%s derived=%s", burnerAddr, derived)
	}
	// `addr` (lowercase) is used in URL paths only — endpoint reads
	// like /accounts/{addr}/state accept any case but we normalise.
	addr := strings.ToLower(burnerAddr)
	if !strings.HasPrefix(addr, "0x") {
		addr = "0x" + addr
	}
	// X-API-Key header carries the NAME of the registered API key, NOT
	// the EVM address. Verified against the live testnet:
	//   GET /accounts/{burner}/api-keys returns
	//     [{"name": "default", "type": "EVM",
	//       "publicKey": "0xcaba55...", "expiresAt": 0}, ...]
	// Sending X-API-Key=<burner-address> (the address) → gateway
	// returns "API key not found" because it looks up keys by NAME.
	// The signed request flow is: gateway looks up `apiKeyName` for
	// the target accountID, recovers the signer from X-API-Sign,
	// verifies the recovered address equals the key's stored
	// `publicKey`. So X-API-Key is the lookup key and the signature
	// is what proves we control the corresponding privkey.
	//
	// `apiKeyName` defaults to "default" — the name the gateway
	// auto-assigns to a master account's first self-registered key.
	// Overridable via SODEX_API_KEY_NAME env var if the operator
	// registered the burner under a different name.
	apiKeyName := strings.TrimSpace(os.Getenv("SODEX_API_KEY_NAME"))
	if apiKeyName == "" {
		apiKeyName = "default"
	}

	probes := []*probeRecord{}

	// --- 1. Spot account state — find aid for the burner.
	spotAid, spotStateRec, err := fetchAccountState("spot", testnetSpotBase, addr)
	if spotStateRec != nil {
		probes = append(probes, spotStateRec)
	}
	if err != nil {
		emitFixture(probes, addr)
		return err
	}

	// --- 2. Spot batch new (limit buy below market, won't fill).
	clOrdSpot := fmt.Sprintf("v3-spot-%d", time.Now().UnixMilli())
	priceStr := testLimitPrice
	qtyStr := testQuantity
	spotNewReq := spotBatchNewOrderRequest{
		AccountID: spotAid,
		Orders: []spotNewOrderItem{{
			SymbolID: testSpotSymbolID, ClOrdID: clOrdSpot,
			Side: 1, Type: 1, TimeInForce: 1,
			Price: &priceStr, Quantity: &qtyStr,
		}},
	}
	spotNonce := uint64(time.Now().UnixMilli())
	spotNewRec, err := submitSignedWrite(
		"spot_batch_new",
		"POST /spot/trade/orders/batch — single limit buy at $100 (below market). Won't fill; gets cancelled by the next probe.",
		http.MethodPost,
		testnetSpotBase+"/trade/orders/batch",
		"newOrder", "spot", testnetChainID, spotNonce,
		spotNewReq, apiKeyName, privKey,
	)
	if spotNewRec != nil {
		probes = append(probes, spotNewRec)
	}
	if err != nil {
		emitFixture(probes, addr)
		return err
	}

	// --- 3. Spot batch cancel — clean up #2 by clOrdID.
	spotCancelReq := spotBatchCancelOrderRequest{
		AccountID: spotAid,
		Cancels: []spotCancelItem{{
			SymbolID: testSpotSymbolID, ClOrdID: clOrdSpot,
			OrigClOrdID: &clOrdSpot,
		}},
	}
	spotCancelNonce := spotNonce + 1
	spotCancelRec, err := submitSignedWrite(
		"spot_batch_cancel",
		"DELETE /spot/trade/orders/batch — cancels the order placed in spot_batch_new by origClOrdID.",
		http.MethodDelete,
		testnetSpotBase+"/trade/orders/batch",
		"cancelOrder", "spot", testnetChainID, spotCancelNonce,
		spotCancelReq, apiKeyName, privKey,
	)
	if spotCancelRec != nil {
		probes = append(probes, spotCancelRec)
	}
	if err != nil {
		emitFixture(probes, addr)
		return err
	}

	// --- 4. Perps account state — separate aid (perps account is distinct).
	perpsAid, perpsStateRec, err := fetchAccountState("perps", testnetPerpsBase, addr)
	if perpsStateRec != nil {
		probes = append(probes, perpsStateRec)
	}
	if err != nil {
		emitFixture(probes, addr)
		return err
	}

	// --- 5. Perps batch new.
	clOrdPerps := fmt.Sprintf("v3-perps-%d", time.Now().UnixMilli())
	perpsNewReq := perpsNewOrderRequest{
		AccountID: perpsAid,
		SymbolID:  testPerpsSymbolID,
		Orders: []perpsOrderItem{{
			ClOrdID:  clOrdPerps,
			Modifier: 1, Side: 1, Type: 1, TimeInForce: 1,
			Price: &priceStr, Quantity: &qtyStr,
			ReduceOnly: false, PositionSide: 1,
		}},
	}
	// Perps signs against "futures" domain per api.md L74.
	perpsNonce := uint64(time.Now().UnixMilli())
	perpsNewRec, err := submitSignedWrite(
		"perps_batch_new",
		"POST /perps/trade/orders — single limit buy at $100 (below market). Won't fill; gets cancelled by the next probe.",
		http.MethodPost,
		testnetPerpsBase+"/trade/orders",
		"newOrder", "futures", testnetChainID, perpsNonce,
		perpsNewReq, apiKeyName, privKey,
	)
	if perpsNewRec != nil {
		probes = append(probes, perpsNewRec)
	}
	if err != nil {
		emitFixture(probes, addr)
		return err
	}

	// --- 6. Perps batch cancel.
	perpsCancelReq := perpsCancelOrderRequest{
		AccountID: perpsAid,
		Cancels: []perpsCancelItem{{
			SymbolID: testPerpsSymbolID,
			ClOrdID:  &clOrdPerps,
		}},
	}
	perpsCancelNonce := perpsNonce + 1
	perpsCancelRec, err := submitSignedWrite(
		"perps_batch_cancel",
		"DELETE /perps/trade/orders — cancels the order placed in perps_batch_new by clOrdID.",
		http.MethodDelete,
		testnetPerpsBase+"/trade/orders",
		"cancelOrder", "futures", testnetChainID, perpsCancelNonce,
		perpsCancelReq, apiKeyName, privKey,
	)
	if perpsCancelRec != nil {
		probes = append(probes, perpsCancelRec)
	}
	if err != nil {
		emitFixture(probes, addr)
		return err
	}

	return emitFixture(probes, addr)
}

// emitFixture writes the captured probes to stdout with burner address
// replaced by the placeholder. Called even on partial failure so the
// operator can debug from the partial fixture.
func emitFixture(probes []*probeRecord, burnerAddr string) error {
	// Scrub the burner address in URLs + headers to the documented
	// placeholder. The actual burner address remains in the
	// burner.json file (unscrubbed) so the operator can re-run.
	placeholder := "<BURNER_ADDR>"
	for _, p := range probes {
		p.URL = strings.ReplaceAll(p.URL, burnerAddr, placeholder)
		for k, v := range p.HeadersSent {
			p.HeadersSent[k] = strings.ReplaceAll(v, burnerAddr, placeholder)
		}
		// Don't scrub the response — testnet response bodies may
		// contain the address but it's documented as scrubbed via
		// `burner_address_placeholder` so consumers know.
	}

	out, err := json.MarshalIndent(map[string]any{
		"schema_version":             "v1",
		"generated_by":               "scripts/sodex_verify/signed_write_capture/main.go",
		"burner_address_placeholder": placeholder,
		"probes":                     probes,
	}, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal output: %w", err)
	}
	_, err = os.Stdout.Write(append(out, '\n'))
	return err
}
