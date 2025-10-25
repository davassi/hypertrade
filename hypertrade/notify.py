import logging

log = logging.getLogger("uvicorn.error")


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """Send a Telegram message via pyTelegramBotAPI.

    - Lazy-imports `telebot` to keep the dependency optional at runtime.
    - Returns True on success, logs and returns False on failure.
    """
    if not token or not chat_id or not text:
        return False
    try:
        # Lazy import to allow tests to stub or projects to run without it until used
        import telebot  # type: ignore
    except Exception:
        log.warning("Telegram library not installed; skipping send")
        return False

    try:
        bot = telebot.TeleBot(token)
        bot.send_message(chat_id, text)
        return True
    except Exception:
        log.warning("Telegram send failed", exc_info=True)
        return False
