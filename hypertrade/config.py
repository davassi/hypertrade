"""Application configuration using Pydantic BaseSettings."""

from functools import lru_cache
from typing import List, Optional
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from env/.env with validation."""

    # ── Core ─────────────────────────────────────
    app_environment: str = "local"
    environment: str  # REQUIRED: must be "prod" or "test" (Hyperliquid API endpoint)
    listen_host: str = "0.0.0.0"
    listen_port: Optional[int] = None

    model_config = SettingsConfigDict(
        env_prefix="HYPERTRADE_",
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
        validate_default=False,
    )

    # ── REQUIRED SECRETS (will crash on import if missing) ─────────────────────
    master_addr: str
    api_wallet_priv: SecretStr
    subaccount_addr: Optional[str] = None  # allowing None to enable trading on master account

    # ── Security & Networking ─────────────────────────────────────────────────
    @property
    def api_url(self) -> str:
        """Derive api_url from HYPERTRADE_ENVIRONMENT."""
        if self.environment == "prod":
            return "https://api.hyperliquid.xyz"
        elif self.environment == "test":
            return "https://api.hyperliquid-testnet.xyz"
        else:
            raise ValueError(
                f"Invalid HYPERTRADE_ENVIRONMENT='{self.environment}'. "
                "Must be 'prod' or 'test'."
            )
            
    ip_whitelist_enabled: bool = False
    # Secure default: do not trust X-Forwarded-For (it is client-spoofable).
    # Enable only when behind a trusted reverse proxy; see hypertrade/security.py.
    trust_forwarded_for: bool = False

    # List[str] env overrides (tv_webhook_ips, rate_limit_*_paths, trusted_hosts)
    # MUST be a JSON array, e.g. HYPERTRADE_TV_WEBHOOK_IPS='["1.2.3.4","5.6.7.8"]'.
    # pydantic-settings' EnvSettingsSource JSON-decodes these before validation;
    # a comma-separated value raises SettingsError at startup, so do NOT add a
    # comma-splitting validator expecting it to work for env input.
    tv_webhook_ips: List[str] = [
        "52.89.214.238",
        "34.212.75.30",
        "54.218.53.128",
        "52.32.178.7",
    ]

    # Logging level
    log_level: str = "INFO"

    # Reduce noisy logs from random scanners
    suppress_404_logs: bool = True

    # Rate limiting (basic in-memory, per-IP)
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_max_requests: int = 120
    rate_limit_burst: int = 30
    rate_limit_only_paths: List[str] = []
    rate_limit_exclude_paths: List[str] = ["/health"]

    # Market order execution premium (basis points)
    # Controls how aggressively orders cross the spread for IOC fills
    # Lower = less slippage cost, but may fail to fill in volatile markets
    # Higher = better fill rate, but higher execution cost
    # Recommended range: 30-60 bps for liquid assets, 60-100 for illiquid
    market_order_premium_bps: int = 40

    # Optional webhook secret; if set, incoming payloads must include
    # `general.secret` matching this value
    webhook_secret: Optional[SecretStr] = None

    # Hardening & limits
    max_payload_bytes: int = 65536
    enable_trusted_hosts: bool = False
    trusted_hosts: List[str] = ["*"]

    # Optional Telegram notifications
    telegram_enabled: bool = True
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # Database persistence
    db_path: str = "./hypertrade_local.db"
    db_enabled: bool = True

    # Cap orders/failures history tables to the most recent N rows (trim-on-insert)
    max_history_rows: int = 200

    # Idempotency (at-most-once order placement keyed on general.nonce)
    idempotency_enabled: bool = True
    idempotency_inflight_timeout: int = 60  # seconds before an in_progress reservation is reclaimable
    # Sweep completed nonces older than this so the dedup index stays bounded.
    # A completed nonce only needs to outlive retries (seconds/minutes); 7 days
    # is a generous safety margin. Must be >= 60s to never race the in-flight window.
    idempotency_retention_seconds: int = 604800  # 7 days

    # Dry-run / demo: accept and fully validate webhooks but never place orders,
    # write to the DB, touch the idempotency store, or send Telegram messages.
    dry_run: bool = False

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("environment")
    @classmethod
    def _validate_hyperliquid_environment(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if value not in {"prod", "test"}:
            raise ValueError("must be 'prod' or 'test'")
        return value

    @field_validator("master_addr")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be set and non-empty")
        return value

    @field_validator("api_wallet_priv")
    @classmethod
    def _secret_not_blank(cls, secret: SecretStr) -> SecretStr:
        if not secret or not secret.get_secret_value().strip():
            raise ValueError("must be set and non-empty")
        return secret

    @field_validator("log_level")
    @classmethod
    def _normalize_level(cls, value: str) -> str:
        level = (value or "").strip().upper()
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        return level if level in valid else "INFO"

    @field_validator("market_order_premium_bps")
    @classmethod
    def _validate_premium_bps(cls, value: int) -> int:
        """Ensure premium is within reasonable range (1-500 bps)."""
        if value < 1:
            raise ValueError("market_order_premium_bps must be at least 1 bps")
        if value > 500:
            raise ValueError("market_order_premium_bps must not exceed 500 bps (5%)")
        return value

    @field_validator("max_history_rows")
    @classmethod
    def _validate_max_history_rows(cls, value: int) -> int:
        """Must keep at least one row; <= 0 would delete everything on insert."""
        if value < 1:
            raise ValueError("max_history_rows must be at least 1")
        return value

    @field_validator("idempotency_retention_seconds")
    @classmethod
    def _validate_idempotency_retention_seconds(cls, value: int) -> int:
        """Keep completed nonces long enough to dedupe retries safely."""
        if value < 60:
            raise ValueError("idempotency_retention_seconds must be at least 60")
        return value

    @model_validator(mode="after")
    def _validate_webhook_authentication(self):
        """Ensure at least one authentication method is enabled for webhook endpoint.

        Either webhook_secret OR ip_whitelist_enabled must be configured to prevent
        unauthorized trading commands from being accepted.
        """
        has_secret = self.webhook_secret is not None and bool(self.webhook_secret.get_secret_value().strip())
        has_ip_whitelist = self.ip_whitelist_enabled

        if not has_secret and not has_ip_whitelist:
            raise ValueError(
                "Webhook authentication required! Must enable at least one of:\n"
                "  • HYPERTRADE_WEBHOOK_SECRET (shared secret authentication)\n"
                "  • HYPERTRADE_IP_WHITELIST_ENABLED=true (IP whitelist authentication)\n"
                "Without authentication, anyone can send trading commands to your account."
            )

        return self

    @model_validator(mode="after")
    def _validate_idempotency_requires_db(self):
        """Idempotency needs the order DB as its dedup store."""
        if self.idempotency_enabled and not self.db_enabled:
            raise ValueError(
                "HYPERTRADE_IDEMPOTENCY_ENABLED=true requires the order DB "
                "(HYPERTRADE_DB_ENABLED=true) as the dedup store."
            )
        return self

# Cached settings instance
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (validates env on first call)."""
    settings = Settings()
    # Force validation of all required fields immediately by copying.
    return settings.model_copy(update=settings.model_dump())
