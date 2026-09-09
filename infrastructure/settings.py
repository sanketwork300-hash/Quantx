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

    # -------------------------------------------------- live market data
    #: Which provider supplies market data: ``synthetic``, ``csv`` or ``upstox``.
    #: There is no automatic fallback to the synthetic market — a deployment
    #: configured for a live provider that cannot be built fails loudly, because
    #: invented prices served as real ones is the worst thing this platform
    #: could do.
    market_data_provider: str = "synthetic"
    market_data_csv_root: Path | None = None

    #: ``polling`` asks the provider's REST API on a timer; ``websocket`` holds
    #: the provider's feed open. Polling is the transport this repository
    #: verifies end to end and states its own sampling interval; the websocket
    #: transport needs a frame decoder for the provider's wire format.
    market_stream_transport: str = "polling"
    market_stream_poll_interval_seconds: float = 1.0
    #: No message for this long, on a connection that is up, is reported as a
    #: STALE feed rather than read as a quiet market.
    market_stream_stale_after_seconds: float = 30.0
    #: How long a live quote survives in the cache without being refreshed.
    #: Short on purpose: an entry that stops being updated must disappear rather
    #: than be served indefinitely as though the feed were still running.
    live_quote_ttl_seconds: int = 300
    #: How long a subscription registered through the API stays live without
    #: being renewed, so interest from a closed browser tab decays on its own.
    live_subscription_ttl_seconds: int = 900
    #: Whether any order from this deployment may reach a real broker.
    #:
    #: Build spec §46 requires this to default to false, and it does. It is one
    #: of two independent gates: an account must also be armed, so neither a
    #: stray configuration change nor a stray API call is enough on its own.
    live_trading_enabled: bool = False

    #: Whose broker connection the shared feed uses. Market data is not
    #: per-user — one entitlement serves the deployment — so the account is
    #: named rather than being whichever user connected first, and it is the
    #: account whose licensing terms govern what may be shown to whom.
    market_data_account_email: str | None = None
    upstox_api_base_url: str = "https://api.upstox.com"

    #: Upstox instrument master. The segment and underlying filters exist
    #: because the complete file covers every listed instrument on every
    #: segment, and no deployment needs all of it.
    upstox_instruments_url: str = (
        "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
    )
    upstox_instrument_segments: str = "NSE_INDEX,NSE_EQ,NSE_FO"
    upstox_instrument_underlyings: str = ""
    #: Multiplies the provider's published tick size. The default of 1 reports
    #: what the provider reported; set it once the unit has been verified.
    upstox_tick_size_scale: float = 1.0

    #: Upstox feed. The socket address is not configured because the provider
    #: does not publish a fixed one: an authorize call returns a short-lived URL.
    upstox_feed_authorize_url: str = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    upstox_feed_mode: str = "full"
    #: Dotted path to the protobuf module generated from the provider's own
    #: ``.proto`` file, and the top-level message in it. Unset means the
    #: websocket transport cannot decode this provider's frames, which is
    #: reported as such rather than worked around.
    upstox_feed_proto_module: str | None = None
    upstox_feed_proto_message: str = "FeedResponse"

    # ------------------------------------------------ historical warehouse
    #: How many partitions a query may pull into memory before refusing. Only
    #: applies to the materialised read path — a filesystem-backed store hands
    #: the files to the reader and prunes without loading them. A query that
    #: would exceed this fails with the number rather than silently truncating.
    warehouse_max_query_partitions: int = 500
    #: Days without an update after which a *continuous* dataset's freshness
    #: score has halved. Historical archives are not scored for freshness at
    #: all: a 2015 tape is not stale, it is history.
    warehouse_freshness_half_life_days: float = 3.0

    # ------------------------------------------------- broker credentials
    #: ``"key_id:base64key[,older_id:base64key]"``. The first entry encrypts new
    #: rows; the rest exist so rows written before a rotation can still be read.
    #: Empty means the platform will not store broker credentials at all.
    credential_encryption_keys: str = ""
    #: How long an authorization handoff stays valid between "Connect" and the
    #: provider redirecting the browser back.
    oauth_state_ttl_minutes: int = 10
    #: Refresh this long before a provider-declared expiry, so a token cannot
    #: lapse between the check and the call that uses it.
    credential_refresh_skew_seconds: int = 120

    #: Upstox app registration. These are issued once when the app is created
    #: and do not rotate daily; the access token they produce is stored in the
    #: database, never here. Endpoint URLs are configuration rather than
    #: constants so that a provider changing them is a deployment change.
    upstox_client_id: str | None = None
    upstox_client_secret: str | None = None
    upstox_redirect_uri: str | None = None
    upstox_authorize_url: str = "https://api.upstox.com/v2/login/authorization/dialog"
    upstox_token_url: str = "https://api.upstox.com/v2/login/authorization/token"

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
    def upstox_segments(self) -> tuple[str, ...]:
        return tuple(
            part.strip() for part in self.upstox_instrument_segments.split(",") if part.strip()
        )

    @property
    def upstox_underlyings(self) -> tuple[str, ...]:
        return tuple(
            part.strip() for part in self.upstox_instrument_underlyings.split(",") if part.strip()
        )

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
        if self.credential_encryption_keys:
            # Parsing here turns a malformed key into a startup failure rather
            # than a failure the first time a user tries to connect a broker.
            from infrastructure.security.crypto import parse_keys

            _, active = parse_keys(self.credential_encryption_keys)
            if active is None:
                raise RuntimeError(
                    "QIP_CREDENTIAL_ENCRYPTION_KEYS is set but contains no usable key."
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
