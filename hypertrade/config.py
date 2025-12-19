"""Application configuration using Pydantic BaseSettings."""

import json
from functools import lru_cache
from typing import List, Optional
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from env/.env with validation."""

    # ── Core ─────────────────────────────────────
    app_name: str = "Hypertrade Daemon"
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
    trust_forwarded_for: bool = True

    # tv_webhook_ips are hardcoded by default, can be overridden in env
    tv_webhook_ips: List[str] = [
        "52.89.214.238",
        "34.212.75.30",
        "54.218.53.128",
        "52.32.178.7",
    ]

    # Logging level
    log_level: str = "INFO"

    # Reduce noisy logs from random scanners
    suppress_access_logs: bool = False
    suppress_404_logs: bool = True
    suppress_invalid_http_warnings: bool = True

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

    @field_validator(
        "tv_webhook_ips",
        "rate_limit_only_paths",
        "rate_limit_exclude_paths",
        "trusted_hosts",
        mode="before")
    @classmethod
    def _parse_path_list(cls, value):
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed]
                except json.JSONDecodeError:
                    pass
            return [part.strip() for part in text.split(",") if part.strip()]
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

# Cached settings instance
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (validates env on first call)."""
    settings = Settings()
    # Force validation of all required fields immediately by copying.
    return settings.model_copy(update=settings.model_dump())
