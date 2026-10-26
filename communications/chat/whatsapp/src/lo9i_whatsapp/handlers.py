"""The user's messages in their chat with themself, sent to lo9i: a command (/new, an agent's /claude <folder>),
an answer to a message's options (choices.py), or a message, with its file and the message it replies to. lo9i
shows what follows in the chat through the stream's operations (channel.py)."""

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from lo9i_whatsapp.choices import Choices
from lo9i_whatsapp.daemon import Attachment, DaemonError
from lo9i_whatsapp.incoming import Incoming, Media

logger = logging.getLogger(__name__)

# /new, /claude ~/code/app; not a path like /Users/ani.
_COMMAND = re.compile(r"^/([A-Za-z][\w-]*)(?:\s+(.*))?$", re.DOTALL)

Download = Callable[[], Awaitable[bytes]]


class DownloadError(Exception):
    """WhatsApp didn't give the message's file. The text says why."""


class Conversation(Protocol):
    """The Daemon calls the handlers need."""

    async def message(
        self,
        chat: str,
        text: str,
        files: Sequence[Attachment] = (),
        voice: bool = False,
        reply_to: str = "",
        message: str = "",
    ) -> None: ...
    async def press(self, chat: str, message: str, text: str, data: str) -> None: ...
    async def command(self, command: str, text: str = "") -> str: ...


class Say(Protocol):
    """The channel's own messages in the chat (port.py)."""

    async def say(self, text: str) -> str: ...


class Handlers:
    def __init__(self, daemon: Conversation, chat: Say, choices: Choices) -> None:
        self._daemon = daemon
        self._chat = chat
        self._choices = choices

    async def message(self, chat: str, incoming: Incoming, download: Download) -> None:
        """`chat` is the user's own chat's id, and `download` fetches the message's file. What lo9i refuses or
        can't take is said in the chat."""
        try:
            await self._handle(chat, incoming, download)
        except DaemonError as e:
            await self._chat.say(f"⚠️ {e}")

    async def _handle(self, chat: str, incoming: Incoming, download: Download) -> None:
        if incoming.unsupported:
            await self._chat.say(incoming.unsupported)
            return
        text = incoming.text.strip()
        if incoming.media is None and (command := _COMMAND.match(text)):
            name, rest = command.group(1), (command.group(2) or "").strip()
            await self._chat.say(await self._daemon.command(name, rest))
            return
        if incoming.media is None and (pick := self._choices.pick(text, incoming.reply_to)):
            await self._daemon.press(chat, pick.offer.message, pick.offer.text, pick.data)
            return
        try:
            files = [await _attachment(incoming.media, download)] if incoming.media is not None else []
        except DownloadError as e:
            await self._chat.say(f"Couldn't download the file from WhatsApp: {e}")
            return
        voice = incoming.media is not None and incoming.media.kind == "voice"
        await self._daemon.message(chat, incoming.text, files, voice, incoming.reply_to, incoming.id)


async def _attachment(media: Media, download: Download) -> Attachment:
    return Attachment(media.name, media.media_type, await download())
