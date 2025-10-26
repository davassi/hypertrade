from functools import lru_cache
from typing import List, Optional
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Application settings using pydantic
class Settings(BaseSettings):
    
    # App defaults
    app_name: str = "Hypertrade Daemon"
    environment: str = "local"

    # Required secrets (must be present in env)
    master_addr: str
    api_wallet_priv: SecretStr
    subaccount_addr: str

    # Optional IP whitelist controls
    ip_whitelist_enabled: bool = False
    trust_forwarded_for: bool = True
    
    # tv_webhook_ips are hardcoded defaults, can be overridden in env
    tv_webhook_ips: List[str] = [
        "52.89.214.238",
        "34.212.75.30",
        "54.218.53.128",
        "52.32.178.7",
    ]

    # Logging
    log_level: str = "INFO"

    # Optional webhook secret; if set, incoming payloads must include `general.secret` matching this value
    webhook_secret: Optional[SecretStr] = None

    # Hardening & limits
    max_payload_bytes: int = 65536
    enable_trusted_hosts: bool = False
    trusted_hosts: List[str] = ["*"]

    # Optional Telegram notifications
    telegram_enabled: bool = True
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # Settings config
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="HYPERTRADE_",
        case_sensitive=False,
    )

    @field_validator("master_addr", "subaccount_addr")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be set and non-empty")
        return v

    @field_validator("api_wallet_priv")
    @classmethod
    def _secret_not_blank(cls, v: SecretStr) -> SecretStr:
        if not v or not v.get_secret_value().strip():
            raise ValueError("must be set and non-empty")
        return v

    @field_validator("tv_webhook_ips", mode="before")
    @classmethod
    def _parse_ip_list(cls, v):
        # Accept list, JSON string, or comma-separated string
        if v is None:
            return v
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("[") and s.endswith("]"):
                import json

                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed]
                except Exception:
                    pass
            # fallback: comma-separated
            return [part.strip() for part in s.split(",") if part.strip()]
        return v

    @field_validator("log_level")
    @classmethod
    def _normalize_level(cls, v: str) -> str:
        level = (v or "").strip().upper()
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        return level if level in valid else "INFO"

# Cached settings instance
@lru_cache
def get_settings() -> Settings:
    # Instantiation validates that required env vars are present
    return Settings()
