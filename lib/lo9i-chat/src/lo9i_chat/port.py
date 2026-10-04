"""The chat operations a turn needs, behind a small interface so rendering can be tested without a
chat service. `M` is the channel's message id (an int in Telegram, a timestamp string in Slack)."""

from typing import Protocol

Buttons = list[tuple[str, str]]  # (label, button data)


class ChatPort[M](Protocol):
    async def send(self, text: str, *, markdown: bool = False, buttons: Buttons | None = None) -> M: ...
    async def edit(self, message_id: M, text: str, *, markdown: bool = False) -> None: ...
    async def typing(self) -> None: ...
