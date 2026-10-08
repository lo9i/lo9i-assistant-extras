"""Telegram updates from paired accounts, sent to lo9i: messages, buttons pressed and commands (/new,
/home, /start, an agent's /claude <folder>). lo9i shows what follows in the chat through the stream's
operations (channel.py)."""

import logging
from collections.abc import Sequence
from typing import Protocol

from telegram import Message, Update
from telegram.ext import ContextTypes

from lo9i_telegram.auth import is_allowed, try_pairing
from lo9i_telegram.daemon import Attachment, DaemonError
from lo9i_telegram.media import UnsupportedMediaError, attachments_from
from lo9i_telegram.pairing import Pairing

logger = logging.getLogger(__name__)


class Conversation(Protocol):
    """The Daemon calls the handlers need."""

    async def message(self, chat: str, text: str, files: Sequence[Attachment] = (), voice: bool = False) -> None: ...
    async def press(self, chat: str, message: str, text: str, data: str) -> None: ...
    async def command(self, command: str, text: str = "") -> str: ...


class Handlers:
    def __init__(self, daemon: Conversation, pairing: Pairing) -> None:
        self._daemon = daemon
        self._pairing = pairing

    async def command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Any command, with what follows it (/claude ~/code/app): lo9i says what to answer."""
        message = update.effective_message
        if message is None or not message.text or not is_allowed(self._pairing, update):
            return
        first, _, rest = message.text.partition(" ")
        name = first.removeprefix("/").split("@")[0]
        await message.reply_text(await self._daemon.command(name, rest.strip()))

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or await try_pairing(self._pairing, update) or not is_allowed(self._pairing, update):
            return
        try:
            attachments = await attachments_from(message)
        except UnsupportedMediaError as e:
            await message.reply_text(str(e))
            return
        text, voice = message.text or message.caption or "", bool(message.voice or message.audio)
        await self._daemon.message(str(message.chat_id), text, attachments, voice)

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not is_allowed(self._pairing, update) or not isinstance(query.message, Message):
            return
        pressed = query.message
        await self._daemon.press(str(pressed.chat_id), str(pressed.message_id), pressed.text or "", query.data or "")

    async def failed(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Python-telegram-bot's error handler: a request lo9i refused or couldn't take is said in the
        chat; anything else is a bug, logged."""
        error = context.error
        message = update.effective_message if isinstance(update, Update) else None
        if isinstance(error, DaemonError) and message is not None:
            await message.reply_text(f"⚠️ {error}")
            return
        logger.error("Telegram update failed", exc_info=error)
