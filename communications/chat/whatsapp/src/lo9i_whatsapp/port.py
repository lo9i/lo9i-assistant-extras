"""The user's chat with themself, as lo9i's chat operations need it, and the limits lo9i keeps there.

Every message the assistant writes starts with PREFIX, so it stands apart from the user's own in a chat where
both are the same account; a message starting with it is never taken for the user's."""

from typing import Protocol

from lo9i_whatsapp.choices import Buttons, Choices, with_options
from lo9i_whatsapp.daemon import Limits
from lo9i_whatsapp.formatting import CHUNK, to_whatsapp

PREFIX = "🤖 "

# Few writes, far apart: no drafts (a reply is sent once, when it's done), the tool calls' status message edited
# at most every 5 seconds, and no voice notes. An account that writes like a person is less likely to be banned.
LIMITS = Limits(edit_seconds=5.0, draft_limit=0, message_limit=CHUNK, voice=False)


class Account(Protocol):
    """What the chat needs from the WhatsApp connection (whatsapp.py): writing in the user's own chat."""

    async def send(self, text: str) -> str: ...
    async def edit(self, message: str, text: str) -> None: ...


class SelfChat:
    def __init__(self, account: Account, choices: Choices) -> None:
        self._account = account
        self._choices = choices

    async def send(self, text: str, markdown: bool = False, buttons: Buttons | None = None) -> str:
        message = await self._account.send(_body(text, markdown, buttons))
        self._choices.offer(message, text, buttons)
        return message

    async def edit(self, message: str, text: str, markdown: bool = False, buttons: Buttons | None = None) -> None:
        """Without buttons, the message's options go, and answering it does nothing any more."""
        self._choices.offer(message, text, buttons)
        await self._account.edit(message, _body(text, markdown, buttons))

    async def say(self, text: str) -> str:
        """A message of the channel's own: a command's answer, or why something didn't work."""
        return await self._account.send(PREFIX + text)


def _body(text: str, markdown: bool, buttons: Buttons | None) -> str:
    return PREFIX + with_options(to_whatsapp(text) if markdown else text, buttons)
