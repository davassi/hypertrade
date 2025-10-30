"""Telegram notification utility using PyTelegramBotAPI."""

import logging
from telebot import TeleBot
from telebot.apihelper import ApiTelegramException


log = logging.getLogger("uvicorn.error")


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """Send a Telegram message; return True on success, False otherwise."""
    if not token or not chat_id or not text:
        return False
    try:
        bot = TeleBot(token)
        bot.send_message(chat_id, text)
        return True
    except ApiTelegramException as exc:
        log.warning("Telegram send failed: %s", exc)
        return False
