"""File-system AI response cache — backtest-only.

Production callers do NOT touch this module. It exists so a `scripts/backtest.py`
sweep over the same date range pays the OpenRouter cost AT MOST ONCE per
unique (detector hit fingerprint, prompt version). Subsequent runs read from
disk and never call the network.

Cache layout (under `<backend>/.backtest_cache/`):

    .backtest_cache/
      v3/                          # AI_PROMPT_VERSION at write time
        a1b2c3d4....json           # DetectorHit.fingerprint (32-hex)

The version directory IS the invalidation seam: bumping `AI_PROMPT_VERSION`
implicitly invalidates the cache (we look up under the new version's dir, find
nothing, refetch). Old version dirs can be deleted manually when an operator
wants to reclaim disk; we don't auto-prune because backtest reproducibility
benefits from keeping history.

Threading: backtest runs single-process, single-async-loop. No file lock —
the file write uses an atomic-rename pattern (`tmp` → `final`) so a crash
mid-write doesn't leave a half-formatted JSON file. Concurrent writers to
the SAME key would race the rename; that's accepted because the value is
deterministic given the key (same hit → same prompt → same response shape).

Anti-drift: this module is the ONLY place that touches `.backtest_cache/`.
Don't add a parallel cache for prices, regimes, or anything else here — those
have their own cache stories (sosovalue TTL, kline lookups against Binance).
This cache is exclusively for AISignalAnalysis JSON.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import structlog

from etfpulse.pipeline.analysis import AI_PROMPT_VERSION, AISignalAnalysis

log = structlog.get_logger()


# Resolves to `<repo>/etfpulse/backend/.backtest_cache/` regardless of where
# the CLI is invoked from. Gitignored. Operators can blow it away safely.
CACHE_ROOT: Path = Path(__file__).resolve().parents[2] / ".backtest_cache"


def _key_path(*, fingerprint: str, prompt_version: str = AI_PROMPT_VERSION) -> Path:
    """Resolve a cache entry's on-disk path. Path is stable across runs."""
    # Defensive: a malformed fingerprint with path separators would let a
    # bad caller write outside the cache root. Detector fingerprints are
    # 32-hex from `compute_fingerprint`; we still validate to keep the
    # module robust against future detectors that might break that shape.
    if "/" in fingerprint or ".." in fingerprint or not fingerprint:
        raise ValueError(f"fingerprint contains illegal path chars: {fingerprint!r}")
    if "/" in prompt_version or ".." in prompt_version or not prompt_version:
        raise ValueError(f"prompt_version contains illegal path chars: {prompt_version!r}")
    return CACHE_ROOT / prompt_version / f"{fingerprint}.json"


def get(*, fingerprint: str, prompt_version: str = AI_PROMPT_VERSION) -> AISignalAnalysis | None:
    """Return the cached analysis for this (fingerprint, version) pair, or None.

    A JSON parse failure or shape-validation failure is treated as a cache miss
    rather than an exception — corrupt cache files should not break a backtest;
    the caller refetches and overwrites on the next put().
    """
    path = _key_path(fingerprint=fingerprint, prompt_version=prompt_version)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AISignalAnalysis.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(
            "ai_cache_corrupt_entry",
            fingerprint=fingerprint,
            prompt_version=prompt_version,
            path=str(path),
            error=str(e),
        )
        return None


def put(
    *,
    fingerprint: str,
    analysis: AISignalAnalysis,
    prompt_version: str = AI_PROMPT_VERSION,
) -> None:
    """Persist an analysis under (fingerprint, version). Atomic via tmp-rename
    so a partial write never surfaces as a half-formatted JSON file on disk."""
    path = _key_path(fingerprint=fingerprint, prompt_version=prompt_version)
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


__all__ = ["CACHE_ROOT", "clear", "get", "put"]
