"""Shows scheduled jobs' approval requests (the daemon's `approval` events) in Telegram. The buttons
are answered in handlers.py."""

from typing import Any

from telegram import Bot
from telegram.error import TelegramError

from lo9i_chat.approvals import job_approval_buttons, job_approval_text
from lo9i_chat.runner import DeliveryError
from lo9i_telegram.port import TelegramChat
from lo9i_telegram.sender import Recipients


class TelegramApprovals:
    def __init__(self, bot: Bot, pairing: Recipients) -> None:
        self._bot = bot
        self._pairing = pairing

    async def forward(self, interrupt_id: str, title: str, request: dict[str, Any]) -> None:
        recipients = self._pairing.recipients()
        if not recipients:
            raise DeliveryError("No Telegram account is paired.")
        text, buttons = job_approval_text(title, request), job_approval_buttons(interrupt_id)
        try:
            for chat_id in recipients:
                await TelegramChat(self._bot, chat_id).send(text, buttons=buttons)
        except TelegramError as e:
            raise DeliveryError(f"Telegram didn't take the request: {e}") from e
