"""Application configuration using Pydantic BaseSettings."""

import json
from functools import lru_cache
from typing import List, Optional
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from env/.env with validation."""

    # ── Core ─────────────────────────────────────
    app_name: str = "Hypertrade Daemon"
    environment: str = "local"
    listen_host: str = "0.0.0.0"
    listen_port: int = 6487

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
    subaccount_addr: str

    # ── Security & Networking ─────────────────────────────────────────────────
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

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("master_addr", "subaccount_addr")
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

# Cached settings instance
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (validates env on first call)."""
    settings = Settings()
    # Force validation of all required fields immediately by copying.
    return settings.model_copy(update=settings.model_dump())
