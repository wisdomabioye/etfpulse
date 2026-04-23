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

    # Telegram
    telegram_bot_token: str = ""

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


settings = Settings()
