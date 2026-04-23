"""Detector primitives — `DetectorHit` and `compute_fingerprint`.

The fingerprint + `signal_date` pair is the idempotency key enforced by the
partial-unique index on `signals`. Two runs of the same detector on the same
inputs MUST produce the same fingerprint, or we double-fire. Resolution R2:
fingerprints are deterministic and contain no timestamps.

Float inputs are banned. Tiny numeric jitter (e.g. `2.0` vs `2.0000001` from
a recomputation) would diverge fingerprints and break idempotency. Detectors
pre-bucket numerics into stable string labels (`"btc_2sigma_long"`), never
pass raw floats.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any

_FINGERPRINT_LEN = 32  # hex chars = 128 bits; matches signals.fingerprint column


@dataclass(frozen=True, slots=True)
class DetectorHit:
    """One candidate signal emitted by a detector.

    `trigger_data` is stored as-is on `Signal.trigger_data` (JSONB) and becomes
    the audit trail + input to AI analysis. Keep it JSON-serialisable (primitives,
    lists, dicts — no Decimals or datetimes).
    """

    signal_type: str
    asset: str
    signal_date: date
    trigger_data: dict[str, Any]
    fingerprint: str


def compute_fingerprint(*parts: str) -> str:
    """SHA-256 of NUL-joined parts, hex-truncated to 32 chars (128 bits).

    Contract:
    - At least one part required.
    - Every part must be a non-empty string. Whitespace-only parts are rejected
      because `"  "` would be indistinguishable from `""` after strip.
    - Parts are `strip()`-ed so trailing/leading whitespace can't cause drift.
    - Parts are joined with NUL (`\\x00`) so `("btc", "long")` cannot collide
      with `("btcl", "ong")` — without a separator, concatenation would be
      ambiguous.
    """
    if not parts:
        raise ValueError("compute_fingerprint requires at least one part")

    cleaned: list[str] = []
    for i, part in enumerate(parts):
        if not isinstance(part, str):
            raise TypeError(f"fingerprint part[{i}] must be str, got {type(part).__name__}")
        stripped = part.strip()
        if not stripped:
            raise ValueError(f"fingerprint part[{i}] is empty after strip")
        cleaned.append(stripped)

    joined = "\x00".join(cleaned)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]
