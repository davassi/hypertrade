"""Telegram notification utility using PyTelegramBotAPI.

This module is defensive with imports so tests can inject a fake
`telebot` module without also providing submodules like
`telebot.apihelper`.
"""

import logging

log = logging.getLogger("uvicorn.error")

# Defensive import: allow tests to inject a minimal `telebot` stub
try:  # pragma: no cover - import shape differs across environments
    from telebot import TeleBot  # type: ignore
    try:  # In some tests a fake `telebot` object is injected without submodules
        from telebot.apihelper import ApiTelegramException  # type: ignore
    except (ImportError, AttributeError):
        class ApiTelegramException(Exception):  # type: ignore
            """Fallback when telebot.apihelper.ApiTelegramException is unavailable."""
except ImportError:  # library might be unavailable in CI
    TeleBot = None  # type: ignore

    class ApiTelegramException(Exception):  # type: ignore
        """Fallback when telebot is not installed in the environment."""


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """Send a Telegram message; return True on success, False otherwise."""
    if not token or not chat_id or not text:
        return False
    if TeleBot is None:  # library unavailable; treat as disabled
        log.debug("telebot not available; skipping Telegram send")
        return False
    try:
        bot = TeleBot(token)  # type: ignore[misc]
        bot.send_message(chat_id, text)
        return True
    except ApiTelegramException as exc:  # type: ignore[name-defined]
        log.warning("Telegram send failed: %s", exc)
        return False
