from decimal import Decimal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Coerce common Postgres URL shapes into `postgresql+asyncpg://`.

    Coolify, Heroku, and other managed platforms inject `DATABASE_URL` with a
    `postgres://` prefix (the historical Heroku alias) or a plain `postgresql://`
    with no driver. SQLAlchemy 1.4+ rejects `postgres://` outright and can't
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
    # password `postgres`). Coolify/Hetzner override via env. Two DBs are expected
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
    # Ratio change required to fire — 0.50 = ±50% acceleration. NOT
    # bounded at 1.0; loose configs may want to demand 2x or 5x.
    acceleration_change_threshold: float = Field(default=0.50, gt=0.0)
    # Floor on the prior window's total flow so a near-zero baseline
    # doesn't produce huge ratios from tiny absolute moves. USD.
    acceleration_min_prior_usd: Decimal = Field(default=Decimal("1000000"), ge=Decimal("0"))
    divergence_lookback_days: int = Field(default=3, ge=1)

    # Bounded wait at scheduler shutdown (issue #28). 10s gives in-flight
    # jobs (a daily cycle, an outcome eval batch) a chance to finish their
    # current DB transaction before SIGTERM forces the event loop down.
    # Coolify's deploy timeout is generous; 10s isn't visible to users and
    # avoids partial-state writes from cancelled transactions. 0 disables
    # the grace entirely (legacy wait=False behaviour).
    scheduler_shutdown_grace_seconds: int = Field(default=10, ge=0)

    # CORS
    cors_origins: str = "http://localhost:5173"

    # Public-facing frontend base URL. Used to build "View on web" deep
    # links into the SPA from Telegram signal alerts (issue #38 inline
    # keyboards). Empty string (the default) disables the button — the
    # alert still sends, just without the link. Distinct from
    # `telegram_public_url` (which is the bot webhook host, not the
    # SPA host — different domains on Vercel/Coolify split deployments).
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
