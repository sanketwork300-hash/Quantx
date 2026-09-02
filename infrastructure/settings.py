"""Typed application settings.

Every configuration value in the platform is read here and nowhere else. No
module calls ``os.environ`` directly; that is what makes configuration
auditable and startup failures loud instead of mysterious.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EXAMPLE_SECRET = "change-me-in-production-use-openssl-rand-hex-32"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class JobExecutionMode(StrEnum):
    QUEUE = "queue"
    EAGER = "eager"


class ObjectStoreBackend(StrEnum):
    LOCAL = "local"
    S3 = "s3"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QIP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------- application
    env: Environment = Environment.DEVELOPMENT
    app_name: str = "Quant Intelligence Platform"
    app_version: str = "0.1.0"
    code_commit: str = "unknown"
    secret_key: str = EXAMPLE_SECRET
    access_token_ttl_minutes: int = 60
    log_level: str = "INFO"
    log_format: str = "json"

    # ------------------------------------------------------------- database
    database_url: str = "sqlite+aiosqlite:///./qip.db"
    database_echo: bool = False
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # ---------------------------------------------------------------- cache
    redis_url: str = "redis://localhost:6379/0"
    cache_default_ttl_seconds: int = 300

    # ------------------------------------------------------------ job queue
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    job_execution_mode: JobExecutionMode = JobExecutionMode.EAGER

    # --------------------------------------------------------- object store
    object_store_backend: ObjectStoreBackend = ObjectStoreBackend.LOCAL
    object_store_root: Path = Path("./var/objectstore")
    s3_endpoint_url: str | None = None
    s3_bucket: str = "qip"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    # -------------------------------------------------------------- uploads
    max_upload_bytes: int = 50 * 1024 * 1024
    max_upload_rows: int = 500_000
    upload_preview_rows: int = 50
    allowed_upload_extensions: tuple[str, ...] = (".csv", ".json", ".parquet", ".txt")

    # --------------------------------------------------------- rate limits
    rate_limit_enabled: bool = False
    auth_rate_limit_per_minute: int = 10
    upload_rate_limit_per_minute: int = 20

    @field_validator("log_format")
    @classmethod
    def _check_log_format(cls, v: str) -> str:
        if v not in {"json", "console"}:
            raise ValueError("log_format must be 'json' or 'console'")
        return v

    @property
    def is_production_like(self) -> bool:
        return self.env in {Environment.STAGING, Environment.PRODUCTION}

    def validate_for_runtime(self) -> None:
        """Refuse to start a production-like process with an example secret.

        Called from the application factory rather than at import time so that
        tooling and tests can import settings freely.
        """
        if self.is_production_like and self.secret_key == EXAMPLE_SECRET:
            raise RuntimeError(
                "QIP_SECRET_KEY is unset or still the example value; refusing to "
                f"start in env={self.env}."
            )
        if self.is_production_like and len(self.secret_key) < 32:
            # HS256 keys shorter than the hash output weaken the signature
            # (RFC 7518 section 3.2).
            raise RuntimeError(
                "QIP_SECRET_KEY must be at least 32 characters; generate one with "
                '`python -c "import secrets; print(secrets.token_hex(32))"`.'
            )
        if self.is_production_like and self.job_execution_mode is JobExecutionMode.EAGER:
            raise RuntimeError(
                "QIP_JOB_EXECUTION_MODE=eager runs long calculations inside the "
                f"request thread; refusing to start in env={self.env}."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: drop the memoized settings so env changes take effect."""
    get_settings.cache_clear()
