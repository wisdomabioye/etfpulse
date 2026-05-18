"""File-system AI response cache — backtest-only.

Production callers do NOT touch this module. It exists so a `scripts/backtest.py`
sweep over the same date range pays the OpenRouter cost AT MOST ONCE per
unique (detector hit fingerprint, prompt version, trigger-data hash). Subsequent
runs read from disk and never call the network.

Cache layout (under `<backend>/.backtest_cache/`):

    .backtest_cache/
      v3/                                  # AI_PROMPT_VERSION at write time
        a1b2c3d4...._t0123abcd.json        # {fingerprint}_{trigger_hash}.json

The cache key has three components:

  * **prompt_version** (directory): bumping `AI_PROMPT_VERSION` implicitly
    invalidates everything — different version, different cohort, different
    prompt shape.
  * **fingerprint** (32-hex): identifies the canonical (asset, signal_type,
    signal_date, bucket) hit. The same fingerprint can fire across many
    backtest runs and still cache-hit, as long as the third component
    matches.
  * **trigger_hash** (PR I.4 — first 16 hex of sha256 over canonicalised
    `DetectorHit.trigger_data`): captures the actual values the AI saw.
    PR I.4 introduced regime-conditional thresholds — under those, a
    `MagnitudeDetector` hit's `trigger_data["percentile"]` shifts with
    the regime multiplier, even when the fingerprint is unchanged. Without
    trigger_hash in the key, a backtest sweep over different multipliers
    would silently reuse stale AI directions keyed only by fingerprint.
    Future regime-conditioning on other detectors gets this defense
    automatically.

Threading: backtest runs single-process, single-async-loop. No file lock —
the file write uses an atomic-rename pattern (`tmp` → `final`) so a crash
mid-write doesn't leave a half-formatted JSON file. Concurrent writers to
the SAME key would race the rename; that's accepted because the value is
deterministic given the key (same hit + same trigger inputs → same prompt
→ same response shape).

Anti-drift: this module is the ONLY place that touches `.backtest_cache/`.
Don't add a parallel cache for prices, regimes, or anything else here — those
have their own cache stories (sosovalue TTL, kline lookups against Binance).
This cache is exclusively for AISignalAnalysis JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

from etfpulse.pipeline.analysis import AI_PROMPT_VERSION, AISignalAnalysis

log = structlog.get_logger()


# Resolves to `<repo>/etfpulse/backend/.backtest_cache/` regardless of where
# the CLI is invoked from. Gitignored. Operators can blow it away safely.
CACHE_ROOT: Path = Path(__file__).resolve().parents[2] / ".backtest_cache"


def hash_trigger_data(trigger_data: dict[str, Any]) -> str:
    """Stable 16-hex hash of a `DetectorHit.trigger_data` dict (PR I.4).

    Used as the third component of the cache key so any drift in what the
    AI would see — regime-driven percentile shifts, future per-detector
    knobs, etc. — invalidates cache entries automatically.

    `sort_keys=True` + `default=str` make the hash:
      * Order-independent across Python dict insertion order.
      * Decimal-safe (`Decimal` is non-serializable by default; `default=str`
        renders it as a string — same form detectors already store as).
    """
    canonical = json.dumps(trigger_data, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _key_path(
    *,
    fingerprint: str,
    trigger_hash: str,
    prompt_version: str = AI_PROMPT_VERSION,
) -> Path:
    """Resolve a cache entry's on-disk path. Path is stable across runs.

    Three required components — see module docstring for cache key rationale.
    """
    # Defensive: a malformed component with path separators would let a
    # bad caller write outside the cache root. Detector fingerprints are
    # 32-hex from `compute_fingerprint` and `trigger_hash` is 16-hex from
    # `hash_trigger_data`; we still validate to keep the module robust
    # against future detectors that might break those shapes.
    for label, value in (
        ("fingerprint", fingerprint),
        ("trigger_hash", trigger_hash),
        ("prompt_version", prompt_version),
    ):
        if "/" in value or ".." in value or not value:
            raise ValueError(f"{label} contains illegal path chars: {value!r}")
    return CACHE_ROOT / prompt_version / f"{fingerprint}_{trigger_hash}.json"


def get(
    *,
    fingerprint: str,
    trigger_hash: str,
    prompt_version: str = AI_PROMPT_VERSION,
) -> AISignalAnalysis | None:
    """Return the cached analysis for this (fingerprint, version) pair, or None.

    A JSON parse failure or shape-validation failure is treated as a cache miss
    rather than an exception — corrupt cache files should not break a backtest;
    the caller refetches and overwrites on the next put().
    """
    path = _key_path(
        fingerprint=fingerprint, trigger_hash=trigger_hash, prompt_version=prompt_version
    )
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AISignalAnalysis.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(
            "ai_cache_corrupt_entry",
            fingerprint=fingerprint,
            trigger_hash=trigger_hash,
            prompt_version=prompt_version,
            path=str(path),
            error=str(e),
        )
        return None


def put(
    *,
    fingerprint: str,
    trigger_hash: str,
    analysis: AISignalAnalysis,
    prompt_version: str = AI_PROMPT_VERSION,
) -> None:
    """Persist an analysis under (fingerprint, trigger_hash, version). Atomic
    via tmp-rename so a partial write never surfaces as a half-formatted JSON
    file on disk."""
    path = _key_path(
        fingerprint=fingerprint, trigger_hash=trigger_hash, prompt_version=prompt_version
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = analysis.model_dump(mode="json")
    # tmp file in the SAME directory so the rename is atomic on POSIX (cross-
    # device renames are not atomic).
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=".tmp_",
        suffix=".json",
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, sort_keys=True, separators=(",", ":"))
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def clear(*, prompt_version: str | None = None) -> int:
    """Operator helper — wipe the cache. Returns the file count removed.

    `prompt_version=None` wipes everything; specifying a version wipes only
    that subdir. Not called from runtime code paths; here so a future
    `--clear-cache` CLI flag has a single place to land.
    """
    if prompt_version is not None:
        target = CACHE_ROOT / prompt_version
        if not target.is_dir():
            return 0
        count = 0
        for f in target.glob("*.json"):
            f.unlink()
            count += 1
        return count
    if not CACHE_ROOT.is_dir():
        return 0
    count = 0
    for f in CACHE_ROOT.rglob("*.json"):
        f.unlink()
        count += 1
    return count


__all__ = ["CACHE_ROOT", "clear", "get", "hash_trigger_data", "put"]
