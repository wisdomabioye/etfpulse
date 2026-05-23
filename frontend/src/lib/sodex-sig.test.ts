/**
 * Golden tests for `toSodexTypedSignature` (#78.2).
 *
 * Pins the byte-exact wire-format contract between wagmi/viem (signer)
 * and the SoDEX gateway (verifier):
 *
 *   1. The 65-byte signature is `0x` + 64 hex r + 64 hex s + 2 hex v.
 *   2. The output format is `0x01` (SoDEX type-byte) + 64 hex r + 64 hex
 *      s + 2 hex v_normalised, where v_normalised ∈ {`00`, `01`}.
 *   3. Acceptable input v bytes: `00`, `01`, `1b`, `1c`. The wallet may
 *      emit either convention; the function normalises to raw
 *      recovery_id (Ethereum legacy `27/28` − 27 = `0/1`).
 *
 * Backend's `_EIP712_SIGNATURE_RE` is `^0x01[0-9a-f]{130}$`. If a future
 * wagmi/viem release changes its default v-byte convention, the FE
 * silently forwards a bad signature and EVERY submit 422s. This test
 * file is the canary.
 */

import { describe, expect, it } from 'vitest';

import { toSodexTypedSignature } from './sodex-sig';

// 64 hex chars of fixed bytes — distinguishable r vs s under inspection.
const R = '11'.repeat(32);
const S = '22'.repeat(32);

describe('toSodexTypedSignature — v-byte normalisation', () => {
  it('accepts v=00 (raw recovery_id 0)', () => {
    const wire = toSodexTypedSignature(`0x${R}${S}00`);
    expect(wire).toBe(`0x01${R}${S}00`);
  });

  it('accepts v=01 (raw recovery_id 1)', () => {
    const wire = toSodexTypedSignature(`0x${R}${S}01`);
    expect(wire).toBe(`0x01${R}${S}01`);
  });

  it('accepts v=1b (Ethereum legacy 27 → recovery_id 0)', () => {
    const wire = toSodexTypedSignature(`0x${R}${S}1b`);
    expect(wire).toBe(`0x01${R}${S}00`);
  });

  it('accepts v=1c (Ethereum legacy 28 → recovery_id 1)', () => {
    const wire = toSodexTypedSignature(`0x${R}${S}1c`);
    expect(wire).toBe(`0x01${R}${S}01`);
  });

  it('uppercase input is lowercased on output', () => {
    // wagmi/viem returns lowercase by default; if a wallet emits
    // mixed case (some do), the gateway's regex requires lowercase.
    const wire = toSodexTypedSignature(`0x${R.toUpperCase()}${S.toUpperCase()}1C`);
    expect(wire).toBe(`0x01${R}${S}01`);
  });
});

describe('toSodexTypedSignature — output shape contract', () => {
  it('always starts with 0x01 SoDEX type-byte', () => {
    const wire = toSodexTypedSignature(`0x${R}${S}1c`);
    expect(wire.startsWith('0x01')).toBe(true);
  });

  it('output length is exactly 134 chars (0x01 + 130 hex)', () => {
    const wire = toSodexTypedSignature(`0x${R}${S}1c`);
    expect(wire).toHaveLength(134);
  });

  it('output matches backend regex ^0x01[0-9a-f]{130}$', () => {
    const wire = toSodexTypedSignature(`0x${R}${S}1c`);
    // Mirror the exact regex from the backend
    // (`pipeline/execution/risk.py:_EIP712_SIGNATURE_RE`).
    expect(wire).toMatch(/^0x01[0-9a-f]{130}$/);
  });

  it('preserves r and s bytes unchanged', () => {
    const wire = toSodexTypedSignature(`0x${R}${S}1c`);
    // Pull r and s back out of the wire format and compare.
    const body = wire.slice(4); // strip "0x01"
    expect(body.slice(0, 64)).toBe(R);
    expect(body.slice(64, 128)).toBe(S);
  });
});

describe('toSodexTypedSignature — rejects malformed input', () => {
  it('rejects missing 0x prefix', () => {
    expect(() => toSodexTypedSignature(`${R}${S}1c`)).toThrow(/0x-prefixed hex/);
  });

  it('rejects too-short signatures', () => {
    expect(() => toSodexTypedSignature('0xdeadbeef')).toThrow(/130 hex chars/);
  });

  it('rejects too-long signatures', () => {
    expect(() => toSodexTypedSignature(`0x${R}${S}1cFF`)).toThrow(/130 hex chars/);
  });

  it('rejects non-hex characters', () => {
    const bogus = 'z'.repeat(128);
    expect(() => toSodexTypedSignature(`0x${bogus}1c`)).toThrow(/non-hex/);
  });

  it('rejects invalid v bytes (e.g., 0x02)', () => {
    expect(() => toSodexTypedSignature(`0x${R}${S}02`)).toThrow(/invalid v byte/);
  });

  it('rejects invalid v bytes (e.g., 0xff)', () => {
    expect(() => toSodexTypedSignature(`0x${R}${S}ff`)).toThrow(/invalid v byte/);
  });

  it('rejects non-string input', () => {
    // Defensive — runtime type check at the FE boundary.
    expect(() => toSodexTypedSignature(null as unknown as string)).toThrow(/0x-prefixed hex/);
    expect(() => toSodexTypedSignature(undefined as unknown as string)).toThrow(/0x-prefixed hex/);
  });
});
