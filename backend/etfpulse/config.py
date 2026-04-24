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

    # SoDEX (Wave 3 — demo wallet only)
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
    # Default user preferences applied on /start registration. Comma-separated
    # asset list parsed via `delivery_default_assets_list` property.
    delivery_default_min_confidence: int = Field(default=6, ge=1, le=10)
    delivery_default_assets: str = "BTC,ETH"

    # CORS
    cors_origins: str = "http://localhost:5173"

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
