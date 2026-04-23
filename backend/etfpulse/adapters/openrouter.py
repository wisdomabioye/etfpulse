"""OpenRouter adapter — converts a detector hit into a typed `AISignalAnalysis`.

Resolution R5: uses `response_format={"type":"json_object"}` so the model
returns parsed JSON, not free-form prose we have to regex.

Resolution R6: `analyze()` NEVER raises into the pipeline. Any failure
(network, 4xx, 5xx, malformed JSON, schema mismatch, empty key, daily cap)
returns `None`, and `signal_builder` decides how to proceed (typically: emit
the signal without ai_analysis, queue for retry tomorrow).

Issue #12: in-memory daily call counter caps spend at
`settings.openrouter_daily_call_cap`. Counter resets at UTC midnight
(matches the cron timezone, see config.py). Process-local — multi-worker
deploys would need a shared counter (Redis); single-process today.

Fixture mode: when `settings.sosovalue_use_fixtures=True` (reused for
single-switch test/demo mode), reads from
`backend/fixtures/openrouter_analysis.json` keyed by `signal_type`.
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
    "no markdown fences, no commentary. Be concise and grounded in the trigger "
    "data; do not invent numbers."
)

_USER_PROMPT_TEMPLATE = (
    "Signal type: {signal_type}\n"
    "Asset: {asset}\n"
    "Trigger data:\n{trigger_data}\n\n"
    "Respond as JSON matching this schema:\n{schema}\n"
)


def _build_messages(
    signal_type: str, asset: str, trigger_data: dict[str, Any]
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _USER_PROMPT_TEMPLATE.format(
                signal_type=signal_type,
                asset=asset,
                trigger_data=json.dumps(trigger_data, indent=2, default=str),
                schema=json.dumps(AISignalAnalysis.model_json_schema(), indent=2),
            ),
        },
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
    ) -> AISignalAnalysis | None:
        """Generate a typed analysis for a detector hit, or None on any failure.

        Resolution R6 — never raises. Failure modes (all → None, all logged):
            - Empty API key (config missing)
            - Daily cap hit
            - Network error / 4xx / 5xx
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
            raw_content = await self._call_chat_completions(signal_type, asset, trigger_data)
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
        self, signal_type: str, asset: str, trigger_data: dict[str, Any]
    ) -> str:
        """POST one chat completion. Returns the raw content string from the
        first choice. Raises OpenRouterError on any HTTP/transport failure.
        """
        body = {
            "model": self.model,
            "messages": _build_messages(signal_type, asset, trigger_data),
            "response_format": {"type": "json_object"},
            "max_tokens": _MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
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

        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str):
            raise OpenRouterError(f"missing string content in first choice: {str(data)[:200]}")
        return content


# Module-level singleton. Tests that need counter isolation should
# instantiate OpenRouterClient() directly.
openrouter_client = OpenRouterClient()
