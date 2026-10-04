"""Keeps the bot's display name in Telegram equal to the assistant's name (the daemon's `hello` and
`renamed` events). The @username stays as set in BotFather."""

import logging

from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class BotName:
    def __init__(self) -> None:
        self._applied: str | None = None

    async def apply(self, bot: Bot, name: str) -> None:
        """Sets the display name if it changed. Telegram limits how often names change, and every
        reconnection to the daemon says the name again, so only real changes are sent; a failure is
        logged and retried at the next one."""
        if not name or name == self._applied:
            return
        try:
            await bot.set_my_name(name)
        except TelegramError as e:
            logger.warning("Couldn't set the Telegram bot's name to %r: %s", name, e)
            return
        self._applied = name
