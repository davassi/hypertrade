"""Telegram settings management schemas."""

from typing import Optional
from pydantic import BaseModel, Field


class TelegramSettingsUpdate(BaseModel):
    """Request schema for updating Telegram settings."""

    enabled: Optional[bool] = Field(None, description="Enable or disable Telegram notifications")
    bot_token: Optional[str] = Field(None, description="Telegram bot token (required if enabling)")
    chat_id: Optional[str] = Field(None, description="Telegram chat ID (required if enabling)")


class TelegramSettingsResponse(BaseModel):
    """Response schema for Telegram settings."""

    status: str = Field(description="Operation status")
    telegram_enabled: bool = Field(description="Whether Telegram notifications are enabled")
    telegram_bot_token: Optional[str] = Field(None, description="Current bot token (masked)")
    telegram_chat_id: Optional[str] = Field(None, description="Current chat ID")
    message: Optional[str] = Field(None, description="Operation message")
