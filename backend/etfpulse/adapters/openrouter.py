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
_REQUEST_TIMEOUT = 60.0  # AI completions are slower than data fetches.
_MAX_TOKENS = 1024  # AISignalAnalysis is small — 1k is comfortable headroom.

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

_SYSTEM_PROMPT = (
    "You are a crypto-market analyst evaluating an ETF flow signal. "
    "Respond ONLY with a JSON object matching the provided schema — no prose, "
    "no markdown fences, no commentary. Be concise and grounded in the "
    "supplied evidence; do not invent numbers. When market regime or recent "
    "news is provided, factor it into your reasoning + risks; when omitted, "
    "rely on trigger data alone."
)


def _build_messages(
    signal_type: str,
    asset: str,
    trigger_data: dict[str, Any],
    regime: RegimeClassification | None = None,
    news_context: list[NewsContextItem] | None = None,
) -> list[dict[str, str]]:
    """Compose the v2 prompt — trigger data + optional regime + optional news.

    Sections appear in fixed order so prompt diffs are reproducible:
        1. Signal identity (type, asset)
        2. Trigger data (always)
        3. Regime classification (optional)
        4. Recent news (optional, max items already capped at gather time)
        5. Output schema

    The regime block surfaces `regime`, `signal_posture`, `confidence`, and
    `macro_events_nearby` only — not the full `reasoning` JSONB, which would
    bloat the prompt and double-count the signals already in trigger_data.
    """
    sections: list[str] = [
        f"Signal type: {signal_type}",
        f"Asset: {asset}",
        "Trigger data:",
        json.dumps(trigger_data, indent=2, default=str),
    ]

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
    ) -> AISignalAnalysis | None:
        """Generate a typed analysis for a detector hit, or None on any failure.

        `regime` and `news_context` are Stage 7-P6 additions — when supplied,
        they're injected into the v2 prompt as additional context. Both
        default to None for backward-compat with callers that haven't been
        updated; the system prompt instructs the model to fall back to
        trigger-data-only reasoning when they're absent.

        Resolution R6 — never raises. Failure modes (all → None, all logged):
            - Empty API key (config missing)
            - Daily cap hit
            - Network error / 4xx / 5xx
            - Mid-stream `choices[0].finish_reason="error"` (200 status,
              provider-side error per OpenRouter docs)
            - Malformed JSON in response
            - JSON parses but doesn't satisfy AISignalAnalysis
            - Fixture mode + missing fixture file/key
        """
        if self.use_fixtures:
            return self._analyze_from_fixture(signal_type)

        if not self.api_key:
            log.warning("openrouter_no_api_key", signal_type=signal_type)
            return None

        try:
            self._check_and_bump_counter()
        except OpenRouterQuotaError:
            return None

        try:
            raw_content = await self._call_chat_completions(
                signal_type, asset, trigger_data, regime, news_context
            )
        except OpenRouterError as exc:
            log.warning(
                "openrouter_request_failed",
                signal_type=signal_type,
                asset=asset,
                error=str(exc),
            )
            return None

        return self._parse_analysis(raw_content, signal_type)

    def _analyze_from_fixture(self, signal_type: str) -> AISignalAnalysis | None:
        entry = self._load_fixture(signal_type)
        if entry is None:
            return None
        return self._parse_analysis(json.dumps(entry), signal_type)

    @staticmethod
    def _parse_analysis(raw_content: str, signal_type: str) -> AISignalAnalysis | None:
        try:
            payload = json.loads(raw_content)
        except ValueError as exc:
            log.warning(
                "openrouter_invalid_json",
                signal_type=signal_type,
                error=str(exc),
                preview=raw_content[:200],
            )
            return None
        try:
            return AISignalAnalysis.model_validate(payload)
        except ValidationError as exc:
            log.warning(
                "openrouter_schema_mismatch",
                signal_type=signal_type,
                errors=exc.errors(),
            )
            return None

    # --- HTTP ---------------------------------------------------------------

    async def _call_chat_completions(
        self,
        signal_type: str,
        asset: str,
        trigger_data: dict[str, Any],
        regime: RegimeClassification | None,
        news_context: list[NewsContextItem] | None,
    ) -> str:
        """POST one chat completion. Returns the raw content string from the
        first choice. Raises OpenRouterError on any HTTP/transport failure
        OR on a mid-200 provider error (`finish_reason="error"`).
        """
        body = {
            "model": self.model,
            "messages": _build_messages(signal_type, asset, trigger_data, regime, news_context),
            "response_format": {"type": "json_object"},
            "max_tokens": _MAX_TOKENS,
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
