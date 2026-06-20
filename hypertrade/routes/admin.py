"""Admin endpoints for managing application settings."""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional

from ..config import get_settings
from ..notify import send_telegram_message
from ..security import require_bearer_secret

log = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["admin"], prefix="/admin")


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


def _mask_secret(secret: str, show_chars: int = 4) -> str:
    """Mask a secret string, showing only the last few characters."""
    if not secret or len(secret) <= show_chars:
        return "***"
    return "*" * (len(secret) - show_chars) + secret[-show_chars:]


@router.post(
    "/telegram",
    response_model=TelegramSettingsResponse,
    summary="Manage Telegram notification settings",
)
async def manage_telegram_settings(
    request: Request,
    settings_update: TelegramSettingsUpdate,
) -> TelegramSettingsResponse:
    """Update Telegram notification settings.

    Requires authentication via webhook secret in Authorization header:
    Authorization: Bearer <webhook_secret>

    Args:
        enabled: Enable/disable Telegram notifications
        bot_token: Telegram bot token (required if enabling)
        chat_id: Telegram chat ID (required if enabling)

    Returns:
        Updated Telegram settings status
    """
    # Validate secret
    require_bearer_secret(request)

    app_settings = request.app.state.settings

    # Handle enable/disable
    if settings_update.enabled is not None:
        if settings_update.enabled and (not settings_update.bot_token or not settings_update.chat_id):
            raise HTTPException(
                status_code=400,
                detail="bot_token and chat_id are required when enabling Telegram"
            )

        app_settings.telegram_enabled = settings_update.enabled

        if settings_update.enabled:
            app_settings.telegram_bot_token = settings_update.bot_token
            app_settings.telegram_chat_id = settings_update.chat_id

            # Update the telegram_notify function in app state
            def _telegram_notify(text: str, _token=settings_update.bot_token, _chat_id=settings_update.chat_id):
                return send_telegram_message(_token, _chat_id, text)

            request.app.state.telegram_notify = _telegram_notify
            log.info("Telegram notifications enabled via admin endpoint")
        else:
            request.app.state.telegram_notify = None
            log.info("Telegram notifications disabled via admin endpoint")

    # Handle token/chat_id updates when already enabled
    elif app_settings.telegram_enabled:
        if settings_update.bot_token:
            app_settings.telegram_bot_token = settings_update.bot_token

        if settings_update.chat_id:
            app_settings.telegram_chat_id = settings_update.chat_id

        # Re-create the notify function if either was updated
        if settings_update.bot_token or settings_update.chat_id:
            token = app_settings.telegram_bot_token
            chat_id = app_settings.telegram_chat_id

            def _telegram_notify(text: str, _token=token, _chat_id=chat_id):
                return send_telegram_message(_token, _chat_id, text)

            request.app.state.telegram_notify = _telegram_notify
            log.info("Telegram notification settings updated via admin endpoint")

    return TelegramSettingsResponse(
        status="ok",
        telegram_enabled=app_settings.telegram_enabled,
        telegram_bot_token=_mask_secret(app_settings.telegram_bot_token) if app_settings.telegram_bot_token else None,
        telegram_chat_id=app_settings.telegram_chat_id,
        message="Telegram settings updated successfully"
    )
