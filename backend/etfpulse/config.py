from decimal import Decimal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Coerce common Postgres URL shapes into `postgresql+asyncpg://`.

    Many managed platforms (Heroku and its lineage) inject `DATABASE_URL`
    with a `postgres://` prefix or a plain `postgresql://` with no driver.
    SQLAlchemy 1.4+ rejects `postgres://` outright and can't
    use the async `asyncpg` driver without the explicit `+asyncpg` suffix.
    This normaliser is the single source of truth — config.py applies it to
    database URLs at load time, and Alembic's env.py reuses it for the `-x db=`
    CLI override path.
    """
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Allow direct construction by field name on top of the aliases —
        # e.g. `Settings(acceleration_min_slope_old_usd=Decimal("..."))`.
        # Without this, fields that declare `validation_alias=AliasChoices(...)`
        # would reject their canonical Python name as an input key, which
        # is a surprise that bites test fixtures and override helpers.
        # (Attribute READ access — `settings.foo` — works regardless.)
        populate_by_name=True,
    )

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Process flags (Gap 29 — separable processes)
    run_bot: bool = True
    run_scheduler: bool = True

    # Database — defaults match a stock local Postgres install (user `postgres`,
    # password `postgres`). Prod platforms override via env. Two DBs are expected
    # on a dev machine: `etfpulse` for the app, `etfpulse_test` for pytest.
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/etfpulse"
    database_url_test: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/etfpulse_test"

    @field_validator("database_url", "database_url_test", mode="before")
    @classmethod
    def _normalize_db_urls(cls, v: str) -> str:
        return normalize_database_url(v)

    # SoSoValue
    sosovalue_api_key: str = ""
    sosovalue_base_url: str = "https://openapi.sosovalue.com/openapi/v1"
    # When True the SoSoValue adapter reads backend/fixtures/*.json instead of
    # making HTTP calls. Tests flip this on via autouse fixture; set to true in
    # .env for offline work.
    sosovalue_use_fixtures: bool = False

    # Binance (backup spot price / klines) — public market-data endpoints only.
    # `api.binance.com` is geo-blocked in some environments; `data-api.binance.vision`
    # is Binance's official market-data-only mirror (no auth, no trading).
    # Issue #34: SoSoValue's monthly quota is the single biggest operational
    # risk — Binance fallback removes the SPOF on spot-price lookups.
    binance_base_url: str = "https://data-api.binance.vision"
    binance_use_fixtures: bool = False

    # SoDEX (Phase 3 — demo wallet only)
    sodex_demo_wallet_address: str = ""
    sodex_demo_private_key: str = ""
    sodex_demo_account_id: int = 0

    # OpenRouter (AI)
    openrouter_api_key: str = ""
    # Verified-present slug on openrouter.ai/api/v1/models (Decision R17).
    openrouter_model: str = "anthropic/claude-sonnet-4.6"
    # Soft daily cap on OpenRouter calls — enforced by signal_builder before
    # invoking the adapter. Issue #12. 0 disables the cap entirely.
    openrouter_daily_call_cap: int = Field(default=100, ge=0)
    # Identifying headers sent with every OpenRouter call — surface this app
    # on OpenRouter's dashboard + rankings. Both are documented as optional
    # but free; we send them for observability. Override per-environment if
    # desired (different prod vs preview attribution).
    openrouter_app_url: str = "https://etfpulse.xpldevelopers.org"
    openrouter_app_title: str = "ETFPulse"
    # Max output tokens per AI call. AISignalAnalysis is small (typically
    # ~400-600 tokens), so the historical 1024 default left comfortable
    # headroom. Exposed as env so low-credit dev/preview deploys can lower
    # this (OpenRouter returns HTTP 402 with "you requested up to N tokens
    # but can only afford M" when account balance can't cover N at the
    # current model's per-token price). 256 is the minimum that reliably
    # fits the JSON schema; below that risks truncation mid-response.
    openrouter_max_tokens: int = Field(default=1024, ge=256, le=8192)

    # Signal scheduler — fires once daily at HH:MM **UTC**. Timezone is pinned
    # to UTC inside the scheduler module (Issue #31), so these are always UTC
    # regardless of host clock. Decision R-cron defaults to 04:30 UTC, ~30 min
    # after SoSoValue's nightly settlement window.
    scheduler_cron_hour: int = Field(default=4, ge=0, le=23)
    scheduler_cron_minute: int = Field(default=30, ge=0, le=59)

    # Telegram bot
    telegram_bot_token: str = ""
    # Webhook security: random URL suffix is the primary defense (unguessable
    # path); secret token is defense-in-depth against URL leakage from logs/
    # ops accidents. Telegram sends the secret in `X-Telegram-Bot-Api-Secret-
    # Token` on every webhook POST when we register it via setWebhook.
    # Generate: `openssl rand -hex 32` for secret, `openssl rand -base64 24` for suffix.
    telegram_webhook_secret: str = ""
    telegram_webhook_url_suffix: str = ""
    # Public base URL (e.g. https://app.example.com) Telegram POSTs to.
    # We assemble {public_url}/api/telegram/webhook/{suffix} and pass to
    # setWebhook. Required by Telegram to be HTTPS.
    telegram_public_url: str = ""

    # Delivery worker — drains queued SignalDelivery rows on this interval.
    # 30s is well under any signal's swing/scalp half-life and keeps Telegram
    # latency invisible to users.
    delivery_worker_interval_seconds: int = Field(default=30, ge=5)
    # Stage 08-P3 — outcome evaluator. Outcomes are scored against DAILY
    # OHLC bars (Stage 08-P2 design); checking more often than once an hour
    # is wasted work because no new bars become available between ticks.
    # 60s floor protects against accidental sub-minute configs in tests.
    outcome_eval_interval_seconds: int = Field(default=3600, ge=60)
    # Per-tick cap on outcome-eval batch size. Each candidate signal triggers
    # one upstream klines fetch (SoSoValue primary, ~100 req/min cap), so an
    # uncapped tick on a fresh deploy with a stranded backlog of 200+
    # signals would risk tripping the rate limit AND exhausting the tick's
    # asyncio time budget. Default 50 leaves comfortable headroom against
    # both — at 50 sequential klines fetches this is ~30s wall time, well
    # within the 1h tick. Steady-state ticks rarely have ≥10 candidates.
    # Bounds: [1, 500]. Values >500 defeat the purpose of a cap; <1 would
    # halt evaluation entirely.
    outcome_eval_batch_limit: int = Field(default=50, ge=1, le=500)
    # Reapers (issues #30 + #36). Both run on the same cadence — neither is
    # latency-sensitive. 15 min is well under any plausible signal half-life
    # while infrequent enough that a flapping reaper doesn't burn cycles.
    # 60s floor for the same reason as outcome_eval (test misconfiguration).
    signal_expiry_reaper_interval_seconds: int = Field(default=900, ge=60)
    delivery_reaper_interval_seconds: int = Field(default=900, ge=60)
    # A SignalDelivery row in PENDING this long after creation is "stuck" —
    # the send worker should have picked it up within ≈20 ticks at the
    # default 30s `delivery_worker_interval_seconds`. Reaper flips it to
    # FAILED with a sentinel error_message so dashboards / debugging see
    # why it didn't deliver. 60s floor protects against test misconfig.
    delivery_pending_max_age_seconds: int = Field(default=600, ge=60)
    # Branch 2 — retry policy for TRANSIENT Telegram errors (rate-limit,
    # 5xx, network). Terminal errors (Blocked, ChatNotFound, ChatMigrated)
    # remain single-attempt with target-side remediation.
    #
    # Total attempts = `delivery_max_attempts` (5 = first try + 4 retries).
    # When `attempt_count` reaches this value AND the send failed
    # transiently, the row flips to FAILED. Below this, the row stays
    # PENDING and the send worker's query waits for the backoff window
    # before re-picking it up.
    #
    # Backoff schedule: `delivery_retry_base_seconds * 2^(attempt_count - 1)`.
    # With defaults (5 attempts, base 30s):
    #     attempt 1 → wait 30s  → attempt 2
    #     attempt 2 → wait 60s  → attempt 3
    #     attempt 3 → wait 120s → attempt 4
    #     attempt 4 → wait 240s → attempt 5 (terminal if still failing)
    # Total worst-case time from first attempt to terminal: 450s (7.5 min).
    # Tune `delivery_retry_base_seconds` to widen / narrow the whole curve.
    delivery_max_attempts: int = Field(default=5, ge=1, le=20)
    delivery_retry_base_seconds: int = Field(default=30, ge=1, le=600)
    # Branch 3 — intra-day cycle. The daily cron at SCHEDULER_CRON_HOUR:MINUTE
    # UTC was the only signal-build trigger pre-Branch-3, which meant ETF
    # data published after 04:30 UTC (or any news arriving during the day)
    # was ignored until the next day's cron. This interval re-runs the same
    # `run_daily_cycle` code path on a fixed schedule, with a skip-guard
    # that short-circuits when no new ETF / news rows landed since last
    # tick (avoids wasted detector queries and OpenRouter spend on the
    # common no-op case).
    #
    # Set to 0 to disable entirely (falls back to daily-cron-only behaviour).
    # Lower bound 15 min protects against accidental sub-15-min configs that
    # could burn through the SoSoValue monthly quota. Upper bound 360 min
    # (6h) — anything coarser is effectively the daily cron with extra cost.
    intraday_cycle_interval_minutes: int = Field(default=60, ge=0, le=360)

    @field_validator("intraday_cycle_interval_minutes")
    @classmethod
    def _validate_intraday_interval(cls, v: int) -> int:
        """Allow `0` (disabled) OR a sensible operational range [15, 360].
        Anything in between (1-14) is almost certainly a misconfiguration
        — sub-15-min cycles risk burning the SoSoValue monthly quota.
        Range upper bound (360 = 6h) is enforced by `Field(le=360)`."""
        if v != 0 and v < 15:
            raise ValueError("intraday_cycle_interval_minutes must be 0 (disabled) or >= 15")
        return v

    # Branch 6 — NULL-confidence auto-retry. When OpenRouter fails at
    # signal-build time (transient quota / network / schema), the signal
    # persists with `ai_analysis IS NULL` and is invisible to fan-out,
    # outcome eval, and the reaper. Branch 6 wires the existing
    # `backfill_null_ai` helper into the intra-day cycle so transient
    # failures self-heal without operator action. The manual endpoint
    # `POST /api/admin/signals/retry-ai` remains for one-off drains.
    ai_backfill_enabled: bool = Field(default=True)
    # Per-tick cap on auto-backfill attempts. Bounds OpenRouter spend on
    # the auto path; the manual endpoint has its own larger cap (50) for
    # operator drain. Default 5 + hourly intra-day = up to 120 retries/day,
    # within `openrouter_daily_call_cap`'s 100 default (the daily cycle
    # also competes; raise the cap or this knob if both run hot).
    ai_backfill_limit_per_tick: int = Field(default=5, ge=1, le=50)
    # Don't retry signals older than this — stale market data isn't worth
    # re-analyzing. 0 disables the age filter (retry any age). Manual
    # operator endpoint ignores this and drains the full backlog.
    ai_backfill_max_age_hours: int = Field(default=24, ge=0, le=720)

    # Admin alert threshold for `signals_null_confidence`. When the live
    # count exceeds this, `AdminMetrics.signals_null_confidence_alert`
    # flips True so ops dashboards can color the counter red. Default 50
    # = "background failure rate no longer looks transient".
    signals_null_confidence_alert_threshold: int = Field(default=50, ge=0)

    # Default user preferences applied on /start registration. Comma-separated
    # asset list parsed via `delivery_default_assets_list` property.
    delivery_default_min_confidence: int = Field(default=6, ge=1, le=10)
    delivery_default_assets: str = "BTC,ETH"

    # Detector thresholds (issue #33). Defaults match the historical
    # constructor args in `pipeline/detectors/*` — change behaviour by
    # setting env vars instead of editing code. Each detector's __init__
    # still accepts overrides explicitly so unit tests can pass tight
    # values without touching settings.
    flow_anomaly_lookback_days: int = Field(default=14, ge=1)
    flow_anomaly_min_streak_length: int = Field(default=3, ge=1)
    magnitude_lookback_days: int = Field(default=90, ge=1)
    # Top-Nth-percentile threshold for "big" days. Strictly fractional —
    # 0 trivially matches every row, 1 matches none.
    magnitude_percentile_threshold: float = Field(default=0.80, gt=0.0, lt=1.0)
    magnitude_min_history_days: int = Field(default=30, ge=1)
    acceleration_window: int = Field(default=7, ge=1)
    # PR F.1 — second-derivative threshold. Default raised from 0.50 (the
    # pre-F.1 first-derivative ratio default) to 1.00 because second
    # derivatives are more volatile and the bar for "trend is genuinely
    # accelerating" should be higher. NOT bounded above 1.0 — tight configs
    # may want 2x or higher to suppress all but the strongest inflections.
    acceleration_change_threshold: float = Field(default=1.00, gt=0.0)
    # Floor on |slope_old| (the prior 7-day → mid 7-day window-sum delta)
    # so a near-zero baseline slope doesn't produce huge ratios from tiny
    # absolute moves. Same numeric-stability role as the pre-F.1 prior-sum
    # floor; semantic shifted to slope under the second-derivative redesign.
    # `gt=0` (not `ge=0`) — a zero floor would let `slope_old=0` data
    # reach the `second_derivative / slope_old` division and crash the
    # cycle with ZeroDivisionError. Boot-time pydantic validation surfaces
    # the misconfiguration before any signal builds. USD.
    #
    # PR #38 rename: previously `acceleration_min_prior_usd`, named after
    # the pre-F.1 semantic (floor on |prior_sum|). F.1 repurposed the
    # value to floor |slope_old| but kept the legacy name; #38 finished
    # the migration. AliasChoices keeps the old env var working so
    # existing deploys don't break on the rename; `config_check.py`
    # warns when only the old name is set so operators migrate.
    acceleration_min_slope_old_usd: Decimal = Field(
        default=Decimal("1000000"),
        gt=Decimal("0"),
        validation_alias=AliasChoices(
            "acceleration_min_slope_old_usd",
            "acceleration_min_prior_usd",
        ),
    )
    divergence_lookback_days: int = Field(default=3, ge=1)
    # PR F.2 — magnitude floors. Divergence is a meaningful signal only when
    # both legs (flows + price) are economically significant. Without these
    # floors, the detector fires on tiny ~0.1% drifts across small flow weeks,
    # which dominated the signal cohort with low-information output (see #76).
    # Defaults: 2% price move + $300M flow volume over the lookback window.
    divergence_min_price_change_pct: float = Field(default=0.02, gt=0.0)
    divergence_min_flow_sum_usd: Decimal = Field(default=Decimal("300000000"), ge=Decimal("0"))

    # Bounded wait at scheduler shutdown (issue #28). 10s gives in-flight
    # jobs (a daily cycle, an outcome eval batch) a chance to finish their
    # current DB transaction before SIGTERM forces the event loop down.
    # The host platform's deploy timeout is generous; 10s isn't visible to
    # users and avoids partial-state writes from cancelled transactions. 0 disables
    # the grace entirely (legacy wait=False behaviour).
    scheduler_shutdown_grace_seconds: int = Field(default=10, ge=0)

    # CORS
    cors_origins: str = "http://localhost:5173"

    # Public-facing frontend base URL. Used to build "View on web" deep
    # links into the SPA from Telegram signal alerts (issue #38 inline
    # keyboards). Empty string (the default) disables the button — the
    # alert still sends, just without the link. Distinct from
    # `telegram_public_url` (which is the bot webhook host, not the
    # SPA host — different domains on split SPA/API deployments).
    #
    # Default is intentionally empty (not the dev localhost) because a
    # mis-configured production would otherwise send users links to
    # `http://localhost:5173`, which would silently break for every
    # recipient. `api/config_check.py` raises a warning when this is
    # empty or localhost-ish in production so the preflight surfaces it
    # before users see broken buttons.
    frontend_url: str = ""

    # Admin
    admin_api_key: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def delivery_default_assets_list(self) -> list[str]:
        """Same shape as `cors_origin_list` — comma-separated string in env,
        list in code. Used as default `User.preferences.assets` on /start."""
        return [a.strip().upper() for a in self.delivery_default_assets.split(",") if a.strip()]

    @property
    def is_bot_enabled(self) -> bool:
        """True iff all required telegram fields are non-empty AND run_bot is on.

        Centralised here so the bot StartupTask, the webhook receiver route,
        and any future delivery code all gate on the same condition without
        drifting. Missing any of the four fields → bot is fully disabled
        (no webhook registered, no route accepts traffic — R12 + W12).
        """
        return bool(
            self.run_bot
            and self.telegram_bot_token
            and self.telegram_public_url
            and self.telegram_webhook_secret
            and self.telegram_webhook_url_suffix
        )


settings = Settings()
