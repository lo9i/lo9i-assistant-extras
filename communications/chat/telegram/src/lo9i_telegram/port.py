"""One Telegram chat, as lo9i's chat operations need it, and the limits lo9i keeps there."""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest

from lo9i_telegram.daemon import Limits
from lo9i_telegram.formatting import CHUNK, to_markdown_v2

logger = logging.getLogger(__name__)

# Telegram allows about one edit per second in a chat.
LIMITS = Limits(edit_seconds=1.0, draft_limit=CHUNK, message_limit=CHUNK, voice=True)

# (label, button data), as lo9i sends them.
Buttons = list[list[str]]


class TelegramChat:
    """Markdown that Telegram rejects is sent again as plain text."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send(self, text: str, markdown: bool = False, buttons: Buttons | None = None) -> int:
        try:
            body, parse_mode = _formatted(text, markdown)
            message = await self._bot.send_message(
                self._chat_id, body, parse_mode=parse_mode, reply_markup=markup(buttons)
            )
        except BadRequest:
            message = await self._bot.send_message(self._chat_id, text, reply_markup=markup(buttons))
        return message.message_id

    async def edit(self, message_id: int, text: str, markdown: bool = False, buttons: Buttons | None = None) -> None:
        """Without buttons, the message's buttons go: pressed ones stop showing."""
        try:
            body, parse_mode = _formatted(text, markdown)
            await self._bot.edit_message_text(
                body, chat_id=self._chat_id, message_id=message_id, parse_mode=parse_mode, reply_markup=markup(buttons)
            )
        except BadRequest as e:
            if "not modified" in str(e):
                return
            await self._edit_plain(message_id, text, buttons)

    async def typing(self) -> None:
        await self._bot.send_chat_action(self._chat_id, ChatAction.TYPING)

    async def voice(self, ogg_opus: bytes) -> None:
        await self._bot.send_voice(self._chat_id, ogg_opus)

    async def _edit_plain(self, message_id: int, text: str, buttons: Buttons | None) -> None:
        try:
            await self._bot.edit_message_text(
                text, chat_id=self._chat_id, message_id=message_id, reply_markup=markup(buttons)
            )
        except BadRequest as e:
            logger.warning("Telegram edit failed: %s", e)


def markup(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    """Approve and Reject side by side; a question's options one per row, so long ones fit."""
    if not buttons:
        return None
    keys = [InlineKeyboardButton(label, callback_data=data) for label, data in buttons]
    return InlineKeyboardMarkup([keys] if len(keys) <= 2 else [[key] for key in keys])


def _formatted(text: str, markdown: bool) -> tuple[str, ParseMode | None]:
    return (to_markdown_v2(text), ParseMode.MARKDOWN_V2) if markdown else (text, None)
