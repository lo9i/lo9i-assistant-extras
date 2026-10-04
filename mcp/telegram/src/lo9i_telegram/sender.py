"""Delivers messages the assistant starts (send_message) to the paired Telegram accounts."""

from typing import Protocol

from telegram import Bot
from telegram.error import TelegramError

from lo9i_chat.runner import DeliveryError
from lo9i_telegram.port import TelegramChat


class Recipients(Protocol):
    """The Pairing call the sender needs."""

    def recipients(self) -> list[int]: ...


class TelegramSender:
    def __init__(self, bot: Bot, pairing: Recipients) -> None:
        self._bot = bot
        self._pairing = pairing

    async def send(self, text: str) -> None:
        recipients = self._pairing.recipients()
        if not recipients:
            raise DeliveryError("No Telegram account is paired yet. The user can pair one in the app's Telegram setup.")
        try:
            for chat_id in recipients:
                await TelegramChat(self._bot, chat_id).send(text, markdown=True)
        except TelegramError as e:
            raise DeliveryError(f"Telegram didn't take the message: {e}") from e
