"""OpenRouter adapter — converts a detector hit into a typed `AISignalAnalysis`.

Resolution R5: uses `response_format={"type":"json_object"}` so the model
returns parsed JSON, not free-form prose we have to regex.

Resolution R6: `analyze()` NEVER raises into the pipeline. Any failure
(network, 4xx, 5xx, malformed JSON, schema mismatch, empty key, daily cap,
mid-stream provider error reported via `choices[0].finish_reason="error"`)
returns `None`, and `signal_builder` decides how to proceed (typically: emit
the signal without ai_analysis, queue for retry tomorrow).

Issue #12: in-memory daily call counter caps spend at
`settings.openrouter_daily_call_cap`. Counter resets at UTC midnight
(matches the cron timezone, see config.py). Process-local — multi-worker
deploys would need a shared counter (Redis); single-process today.

Fixture mode: when `settings.sosovalue_use_fixtures=True` (reused for
single-switch test/demo mode), reads from
`backend/fixtures/openrouter_analysis.json` keyed by `signal_type`.

Stage 7-P6: prompt v2 — `analyze()` now accepts optional `regime` (a
RegimeClassification) and `news_context` (a list of dict rows from
`pipeline.news_context`). Both default to None for backward compatibility;
when present they're injected as additional context blocks in the user
prompt. Identifying headers (`HTTP-Referer`, `X-OpenRouter-Title`) are
also sent so OpenRouter rankings reflect this app.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import structlog
from pydantic import ValidationError

from etfpulse.config import settings
from etfpulse.pipeline.analysis import AISignalAnalysis

# `RegimeClassification` is a frozen dataclass with no DB-engine coupling —
# safe to import here even though it lives in `pipeline/`. The existing
# `analysis.py` import already establishes that adapters can read pipeline
# data shapes (no cycle: regime_monitor imports adapters.sosovalue, never
# adapters.openrouter). `NewsContextItem` is a TypedDict — same story.
from etfpulse.pipeline.news_context import NewsContextItem
from etfpulse.pipeline.regime_monitor import RegimeClassification

log = structlog.get_logger()


_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _strip_markdown_fence(raw: str) -> str:
    """Strip a leading/trailing ```json...``` (or plain ```...```) wrapper.

    Some OpenRouter providers ignore `response_format: json_object` and
    wrap the response in a markdown fenced code block. The wrapper varies:
    ` ```json\\n…\\n``` `, ` ```\\n…\\n``` `, or with trailing whitespace.
    `json.loads` chokes on the fence chars; this helper unwraps so the
    payload parses cleanly.

    Returns `raw` unchanged when no fence is present — safe to call
    unconditionally on every model response.
    """
    s = raw.strip()
    if not s.startswith("```"):
        return raw
    # Drop the opening fence line: ```json or ``` (with optional language tag).
    nl = s.find("\n")
    if nl == -1:
        return raw  # one-line ``` — bail; nothing useful to unwrap
    body = s[nl + 1 :]
    # Drop the trailing fence — must end with ``` (possibly with trailing space).
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


_REQUEST_TIMEOUT = 60.0  # AI completions are slower than data fetches.
# Default max_tokens — overridden per-call by `settings.openrouter_max_tokens`
# (env-tunable for low-credit dev/preview deploys; see config.py comment).
# Kept as a module-level fallback for tests that instantiate the client
# without going through pydantic-settings.
_MAX_TOKENS = 1024

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Exceptions — internal signaling only. `analyze()` catches everything.
# ---------------------------------------------------------------------------


class OpenRouterError(Exception):
    """Base class for adapter errors. Caller never sees this — `analyze()` returns None."""


class OpenRouterQuotaError(OpenRouterError):
    """Daily call cap exhausted. Retrying today is pointless."""


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# Stage 8-P1 — the v3 system prompt teaches the LLM the entry/stop/target
# rules. The "current spot" anchor referenced below is fed in as its own
# prompt section by `_build_messages` when the caller passes `current_price`
# (`signal_builder` threads `price_at_creation` here so the model has a
# real number to anchor to instead of guessing from training data).
_SYSTEM_PROMPT = (
    "You are a crypto-market analyst evaluating an ETF flow signal. "
    "Respond ONLY with a JSON object matching the provided schema — no prose, "
    "no markdown fences, no commentary. Be concise and grounded in the "
    "supplied evidence; do not invent numbers. When market regime or recent "
    "news is provided, factor it into your reasoning + risks; when omitted, "
    "rely on trigger data alone.\n\n"
    "PRICE LEVELS: when suggested_action is 'consider long' or "
    "'consider short', also provide entry_price, stop_price, and target_price "
    "as positive decimals in the asset's USD quote. Rules:\n"
    "  - entry_price: at or within 1% of the 'Current spot price' below "
    "(if provided); pick a nearby support/resistance level inside that band "
    "when one is obvious. If no spot price is supplied, return null.\n"
    "  - stop_price: BELOW entry for longs, ABOVE entry for shorts. Sized so "
    "that risk per trade is roughly 1-2% of entry — tighter for 'scalp', "
    "looser for 'position'.\n"
    "  - target_price: aims for a risk:reward of at least 1:1.5. ABOVE entry "
    "for longs, BELOW entry for shorts.\n"
    "  - When suggested_action is 'wait', set all three to null. Schema "
    "validators will null them anyway, but explicit nulls keep your output "
    "self-consistent."
)


def _build_messages(
    signal_type: str,
    asset: str,
    trigger_data: dict[str, Any],
    regime: RegimeClassification | None = None,
    news_context: list[NewsContextItem] | None = None,
    current_price: Decimal | None = None,
) -> list[dict[str, str]]:
    """Compose the v3 prompt — trigger data + optional regime/news/spot price.

    Sections appear in fixed order so prompt diffs are reproducible:
        1. Signal identity (type, asset)
        2. Current spot price (optional — Stage 8-P1; required for the v3
           system prompt's entry/stop/target rules to anchor on real
           numbers rather than the LLM's training-data ballpark)
        3. Trigger data (always)
        4. Regime classification (optional)
        5. Recent news (optional, max items already capped at gather time)
        6. Output schema (now includes entry/stop/target Decimal fields — Stage 8-P1)

    The regime block surfaces `regime`, `signal_posture`, `confidence`, and
    `macro_events_nearby` only — not the full `reasoning` JSONB, which would
    bloat the prompt and double-count the signals already in trigger_data.

    Schema embedding uses `AISignalAnalysis.model_json_schema()` so adding
    fields to the Pydantic class automatically updates the prompt — no
    manual schema-string drift between the validator and the prompt.
    """
    sections: list[str] = [
        f"Signal type: {signal_type}",
        f"Asset: {asset}",
    ]
    if current_price is not None:
        # Plain string (not JSON-wrapped) — the system prompt references it
        # by the literal label "Current spot price" so the LLM can find it
        # regardless of which trigger_data shape is also in the prompt.
        sections.append(f"Current spot price (USD): {current_price}")
    sections.append("Trigger data:")
    sections.append(json.dumps(trigger_data, indent=2, default=str))

    if regime is not None:
        regime_block = {
            "regime": regime.regime.value,
            "signal_posture": regime.signal_posture.value,
            "confidence": regime.confidence,
            "macro_events_nearby": list(regime.macro_events_nearby),
        }
        sections.append("\nMarket regime classification:")
        sections.append(json.dumps(regime_block, indent=2))

    if news_context:
        sections.append("\nRecent relevant news (most recent first):")
        sections.append(json.dumps(news_context, indent=2, default=str))

    sections.append("\nRespond as JSON matching this schema:")
    sections.append(json.dumps(AISignalAnalysis.model_json_schema(), indent=2))

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(sections)},
    ]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenRouterClient:
    """Thin async wrapper for OpenRouter chat-completions.

    One client holds the daily-call counter; tests should instantiate fresh
    clients to avoid counter bleed-through. The module singleton at the
    bottom is what the pipeline uses in production.
    """

    def __init__(self) -> None:
        self._call_count: int = 0
        self._counter_date: date = self._utc_today()

    @property
    def use_fixtures(self) -> bool:
        return settings.sosovalue_use_fixtures

    @property
    def api_key(self) -> str:
        return settings.openrouter_api_key

    @property
    def model(self) -> str:
        return settings.openrouter_model

    @property
    def daily_cap(self) -> int:
        return settings.openrouter_daily_call_cap

    @staticmethod
    def _utc_today() -> date:
        return datetime.now(UTC).date()

    # --- Daily cap ----------------------------------------------------------

    def _check_and_bump_counter(self) -> None:
        """Reset on UTC day rollover, then enforce the cap."""
        today = self._utc_today()
        if today != self._counter_date:
            log.info("openrouter_counter_reset", previous_count=self._call_count, date=str(today))
            self._call_count = 0
            self._counter_date = today

        # Cap of 0 = disabled.
        if self.daily_cap > 0 and self._call_count >= self.daily_cap:
            log.warning("openrouter_daily_cap_hit", count=self._call_count, cap=self.daily_cap)
            raise OpenRouterQuotaError(f"daily cap {self.daily_cap} reached at {self._call_count}")

        # Bump BEFORE the call so failed calls still count — a flapping API
        # can't blow past the cap by retrying.
        self._call_count += 1

    # --- Fixtures -----------------------------------------------------------

    @staticmethod
    def _load_fixture(signal_type: str) -> dict[str, Any] | None:
        """Read backend/fixtures/openrouter_analysis.json keyed by signal_type.

        Returns None on missing file/key so the caller treats it as a normal
        adapter failure (R6).
        """
        path = FIXTURES_DIR / "openrouter_analysis.json"
        if not path.exists():
            log.warning("openrouter_fixture_missing", path=str(path))
            return None
        try:
            data = cast(dict[str, Any], json.loads(path.read_text()))
        except ValueError as exc:
            log.error("openrouter_fixture_invalid_json", error=str(exc))
            return None
        entry = data.get(signal_type)
        if entry is None:
            log.warning("openrouter_fixture_missing_key", signal_type=signal_type)
        return cast(dict[str, Any] | None, entry)

    # --- Public API ---------------------------------------------------------

    async def analyze(
        self,
        signal_type: str,
        asset: str,
        trigger_data: dict[str, Any],
        regime: RegimeClassification | None = None,
        news_context: list[NewsContextItem] | None = None,
        current_price: Decimal | None = None,
    ) -> AISignalAnalysis | None:
        """Generate a typed analysis for a detector hit, or None on any failure.

        Existing R6 contract — returns None on any failure, all reasons
        logged but not surfaced to the caller. Used by the daily cycle
        (`signal_builder.build_signal`) where a failed enrichment is
        tolerated silently and the signal persists without AI.

        Callers that need to report the failure reason (e.g. the admin
        retry-AI backfill) should use `analyze_with_reason()` instead —
        it returns `(analysis, reason)` so a UI can show the operator
        why a retry didn't take.
        """
        analysis, _reason = await self.analyze_with_reason(
            signal_type, asset, trigger_data, regime, news_context, current_price
        )
        return analysis

    async def analyze_with_reason(
        self,
        signal_type: str,
        asset: str,
        trigger_data: dict[str, Any],
        regime: RegimeClassification | None = None,
        news_context: list[NewsContextItem] | None = None,
        current_price: Decimal | None = None,
    ) -> tuple[AISignalAnalysis | None, str | None]:
        """Same orchestration as `analyze()` but returns the failure reason.

        Returns `(analysis, None)` on success and `(None, reason)` on every
        documented failure mode below. The reason is short, operator-facing,
        and safe to surface in admin UIs (no API keys or large response
        bodies leaked — preview is bounded to 200 chars).

        Failure modes (all → `(None, reason)`, all logged):
            - "missing OpenRouter API key"
            - "daily call cap reached"
            - "request failed: <err>" (network / 4xx / 5xx / mid-stream)
            - "invalid JSON in response: <err>"
            - "response did not match AISignalAnalysis schema"
            - "fixture missing for signal_type=…" (fixture mode)

        `regime` / `news_context` / `current_price` are forwarded to the
        prompt builder unchanged — see `analyze()` for their semantics.
        """
        if self.use_fixtures:
            entry = self._load_fixture(signal_type)
            if entry is None:
                return None, f"fixture missing for signal_type={signal_type}"
            return self._parse_analysis(json.dumps(entry), signal_type)

        if not self.api_key:
            log.warning("openrouter_no_api_key", signal_type=signal_type)
            return None, "missing OpenRouter API key"

        try:
            self._check_and_bump_counter()
        except OpenRouterQuotaError as exc:
            return None, f"daily call cap reached: {exc}"

        try:
            raw_content = await self._call_chat_completions(
                signal_type, asset, trigger_data, regime, news_context, current_price
            )
        except OpenRouterError as exc:
            log.warning(
                "openrouter_request_failed",
                signal_type=signal_type,
                asset=asset,
                error=str(exc),
            )
            return None, f"request failed: {str(exc)[:160]}"

        return self._parse_analysis(raw_content, signal_type)

    @staticmethod
    def _parse_analysis(
        raw_content: str, signal_type: str
    ) -> tuple[AISignalAnalysis | None, str | None]:
        """Parse + validate the model response. Returns `(analysis, None)` on
        success, `(None, reason)` on parse / schema failure.

        Strips markdown ```json fences before parsing — some OpenRouter
        providers don't strictly honor `response_format: json_object` and
        wrap the JSON in a fenced code block. Without this strip, parsing
        chokes on the leading ` ``` ` and the row gets stranded with a
        misleading "invalid JSON" error.
        """
        cleaned = _strip_markdown_fence(raw_content)
        try:
            payload = json.loads(cleaned)
        except ValueError as exc:
            log.warning(
                "openrouter_invalid_json",
                signal_type=signal_type,
                error=str(exc),
                preview=raw_content[:200],
            )
            return None, f"invalid JSON in response: {exc}"
        try:
            return AISignalAnalysis.model_validate(payload), None
        except ValidationError as exc:
            log.warning(
                "openrouter_schema_mismatch",
                signal_type=signal_type,
                errors=exc.errors(),
            )
            # Pydantic's full error list can be long; surface only the first
            # few errors to keep the operator UI readable.
            first_errs = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:2]
            )
            return None, f"schema mismatch: {first_errs}"

    # --- HTTP ---------------------------------------------------------------

    async def _call_chat_completions(
        self,
        signal_type: str,
        asset: str,
        trigger_data: dict[str, Any],
        regime: RegimeClassification | None,
        news_context: list[NewsContextItem] | None,
        current_price: Decimal | None,
    ) -> str:
        """POST one chat completion. Returns the raw content string from the
        first choice. Raises OpenRouterError on any HTTP/transport failure
        OR on a mid-200 provider error (`finish_reason="error"`).
        """
        body = {
            "model": self.model,
            "messages": _build_messages(
                signal_type, asset, trigger_data, regime, news_context, current_price
            ),
            "response_format": {"type": "json_object"},
            "max_tokens": settings.openrouter_max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Identifying headers — improve OpenRouter rankings/attribution
            # and make our usage debuggable on their dashboard. Both are
            # documented as optional; we send both for completeness.
            "HTTP-Referer": settings.openrouter_app_url,
            "X-OpenRouter-Title": settings.openrouter_app_title,
        }

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            try:
                response = await client.post(
                    f"{_OPENROUTER_BASE_URL}/chat/completions",
                    headers=headers,
                    json=body,
                )
            except httpx.RequestError as exc:
                raise OpenRouterError(f"network error: {exc}") from exc

        if response.status_code == 429:
            # OpenRouter doesn't distinguish per-minute vs daily quota in the
            # status alone; treat any 429 as "back off, return None upstream".
            raise OpenRouterError(f"HTTP 429: {response.text[:200]}")

        if response.status_code >= 400:
            raise OpenRouterError(f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise OpenRouterError(f"invalid JSON envelope: {exc}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise OpenRouterError(f"no choices in response: {str(data)[:200]}")

        first_choice = choices[0]

        # Mid-stream provider error (per OpenRouter docs): 200 OK with
        # `finish_reason="error"` and a populated `error` field on the
        # choice. Treat it the same as a 4xx/5xx — log + return None.
        if first_choice.get("finish_reason") == "error":
            err_payload = first_choice.get("error") or {}
            raise OpenRouterError(f"provider error mid-response: {json.dumps(err_payload)[:200]}")

        content = first_choice.get("message", {}).get("content")
        if not isinstance(content, str):
            raise OpenRouterError(f"missing string content in first choice: {str(data)[:200]}")
        return content


# Module-level singleton. Tests that need counter isolation should
# instantiate OpenRouterClient() directly.
openrouter_client = OpenRouterClient()
