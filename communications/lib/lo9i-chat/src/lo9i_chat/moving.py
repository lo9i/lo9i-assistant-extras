"""Moving a task out of the home conversation once the user took the agent's suggestion (side.py):
the channel starts a side conversation and sends the brief there, so the agent picks the task up."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from lo9i_chat.attachments import Attachment
from lo9i_chat.events import Event
from lo9i_chat.port import ChatPort
from lo9i_chat.turn import ChatTurn, Limits


class Conversations(Protocol):
    """The ChannelClient calls moving needs."""

    def message(self, text: str, files: Sequence[Attachment] = ()) -> AsyncIterator[Event]: ...
    async def new_conversation(self) -> str: ...


async def move_to_side[M](client: Conversations, chat: ChatPort[M], limits: Limits, first_message: str) -> None:
    """Starts the side conversation and sends the brief there, so the agent picks the task up."""
    await client.new_conversation()
    await chat.send("Moved to a side conversation. /home goes back.")
    await ChatTurn(chat, limits).render(client.message(first_message))
