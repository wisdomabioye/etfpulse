/**
 * Signature wire-format normalization for SoDEX EIP-712 submission.
 *
 * Wallet ↔ gateway impedance mismatch:
 *   - viem `signTypedData` returns a 65-byte ECDSA signature as a
 *     0x-prefixed hex string (132 chars). The trailing byte (`v`) is
 *     `27` (0x1b) or `28` (0x1c) per the Ethereum convention.
 *   - The SoDEX gateway expects `v ∈ {0, 1}` (raw recovery_id) AND a
 *     `0x01` SoDEX type-byte prefix glued to the front of the 130 hex
 *     chars. The backend's `_EIP712_SIGNATURE_RE` is
 *     `^0x01[0-9a-f]{130}$` — anything else 422s before risk runs.
 *
 * `toSodexTypedSignature` does both transforms with a single pass:
 *   1. Strip the `0x` prefix.
 *   2. If the last byte is 0x1b/0x1c, subtract 27.
 *   3. Lowercase + prepend `0x01`.
 *
 * Pure function, no side effects, fully unit-testable.
 *
 * See CLAUDE.md "SoDEX HTTP adapters (D.2)" §wire-contract finding #3:
 * "Signature `v` byte is raw `{0, 1}` — no `+27` offset. wagmi/viem
 *  returns `v ∈ {27, 28}` by default, so D.4's frontend → backend
 *  forwarding will need a `v -= 27` normalisation step."
 */

/**
 * Convert a wagmi/viem `Hex` signature (0x-prefixed 65-byte ECDSA)
 * into the SoDEX gateway's typed-signature wire format.
 *
 * Throws if the input doesn't look like a 65-byte hex signature —
 * fails loud at the FE boundary rather than letting a malformed
 * value race the backend's 422.
 */
export function toSodexTypedSignature(signature: string): string {
  if (typeof signature !== 'string' || !signature.startsWith('0x')) {
    throw new Error(`toSodexTypedSignature: expected 0x-prefixed hex, got ${signature}`);
  }
  const body = signature.slice(2).toLowerCase();
  if (body.length !== 130) {
    throw new Error(
      `toSodexTypedSignature: expected 65-byte (130 hex chars) signature, got ${body.length} chars`,
    );
  }
  if (!/^[0-9a-f]+$/.test(body)) {
    throw new Error('toSodexTypedSignature: non-hex characters in signature');
  }

  const r = body.slice(0, 64);
  const s = body.slice(64, 128);
  const vHex = body.slice(128, 130);
  const v = parseInt(vHex, 16);

  // Normalise v to raw recovery_id ∈ {0, 1}. Accept either wire form:
  //   - 0/1     — already raw recovery_id (some libs/wallets emit this)
  //   - 27/28   — Ethereum legacy convention (viem default)
  // Anything else is a malformed signature.
  let recoveryId: number;
  if (v === 0 || v === 1) {
    recoveryId = v;
  } else if (v === 27 || v === 28) {
    recoveryId = v - 27;
  } else {
    throw new Error(`toSodexTypedSignature: invalid v byte 0x${vHex}; expected 00/01/1b/1c`);
  }

  const vNormalized = recoveryId === 0 ? '00' : '01';
  return `0x01${r}${s}${vNormalized}`;
}
