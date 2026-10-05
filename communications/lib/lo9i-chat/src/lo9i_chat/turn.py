"""Shows one run's events in a chat: a typing indicator, a status message listing tool calls,
the reply streamed by editing its message, and approval or question buttons."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from lo9i_chat.approvals import approval_buttons, approval_text
from lo9i_chat.events import (
    ApprovalRequired,
    Cancelled,
    Done,
    Error,
    Event,
    TextDelta,
    ToolFinished,
    ToolStarted,
    Transcribed,
    is_question,
    is_side_conversation,
)
from lo9i_chat.port import ChatPort
from lo9i_chat.questions import question_buttons, question_text
from lo9i_chat.side import side_buttons, side_text
from lo9i_chat.split import split_text

_TYPING_INTERVAL = 4.0


@dataclass(frozen=True)
class Limits:
    """A channel's pace and sizes."""

    # Seconds between edits of one message; chat services limit how often a bot edits.
    edit_interval: float
    # Drafts are shown while the reply is at most this long; past it, only the final version.
    stream_limit: int
    # The longest final message; longer replies are split.
    chunk: int


class ChatTurn[M]:
    def __init__(self, chat: ChatPort[M], limits: Limits) -> None:
        self._chat = chat
        self._limits = limits
        self._status = _StatusMessage(chat, limits)
        self._reply = _ReplyMessage(chat, limits)
        self._final = ""

    @property
    def final_text(self) -> str:
        """The reply after the last tool call: the answer, without text written while working."""
        return self._final.strip()

    async def render(self, events: AsyncIterator[Event]) -> None:
        typing = asyncio.create_task(self._keep_typing())
        try:
            async for event in events:
                await self._handle(event)
        finally:
            typing.cancel()
            await self._reply.finish()
            await self._status.flush(force=True)

    async def _handle(self, event: Event) -> None:
        match event:
            case TextDelta(text):
                self._final += text
                await self._reply.append(text)
            case ToolStarted(name, args):
                self._final = ""
                await self._after_reply()
                await self._status.started(name, args)
            case ToolFinished(name, _):
                await self._status.finished(name)
            case ApprovalRequired(interrupt_id, request) if is_side_conversation(request):
                await self._after_reply()
                await self._chat.send(side_text(request), buttons=side_buttons(interrupt_id))
            case ApprovalRequired(interrupt_id, request) if is_question(request):
                await self._after_reply()
                await self._chat.send(question_text(request), buttons=question_buttons(interrupt_id, request))
            case ApprovalRequired(interrupt_id, request):
                await self._after_reply()
                await self._chat.send(approval_text(request), buttons=approval_buttons(interrupt_id))
            case Transcribed(text, language):
                await self._chat.send(f"🎤 I heard ({language}): {text or '(no speech)'}")
            case Error(message):
                await self._chat.send(f"⚠️ {message}")
            case Cancelled():
                await self._chat.send("⏹ Stopped.")
            case Done():
                pass

    async def _after_reply(self) -> None:
        """Finish any reply in progress. Tool calls after it go in a new status message, below it."""
        if await self._reply.finish():
            await self._status.flush(force=True)
            self._status = _StatusMessage(self._chat, self._limits)

    async def _keep_typing(self) -> None:
        while True:
            await self._chat.typing()
            await asyncio.sleep(_TYPING_INTERVAL)


class _StatusMessage[M]:
    """One message listing this turn's tool calls, edited as they start and finish."""

    def __init__(self, chat: ChatPort[M], limits: Limits) -> None:
        self._chat = chat
        self._limits = limits
        self._lines: list[list[str]] = []  # [marker, name, description]
        self._message_id: M | None = None
        self._last_edit = 0.0

    async def started(self, name: str, args: dict[str, Any]) -> None:
        self._lines.append(["⏺", name, _describe(args)])
        await self.flush()

    async def finished(self, name: str) -> None:
        for line in self._lines:
            if line[0] == "⏺" and line[1] == name:
                line[0] = "✓"
                break
        await self.flush()

    async def flush(self, force: bool = False) -> None:
        if not self._lines or (not force and time.monotonic() - self._last_edit < self._limits.edit_interval):
            return
        text = "\n".join(f"{marker} {name}({description})" for marker, name, description in self._lines)
        if self._message_id is None:
            self._message_id = await self._chat.send(text)
        else:
            await self._chat.edit(self._message_id, text)
        self._last_edit = time.monotonic()


class _ReplyMessage[M]:
    """Streams text by editing one message; the final version is sent as Markdown and split if long."""

    def __init__(self, chat: ChatPort[M], limits: Limits) -> None:
        self._chat = chat
        self._limits = limits
        self._text = ""
        self._message_id: M | None = None
        self._last_edit = 0.0

    async def append(self, text: str) -> None:
        self._text += text
        if (
            len(self._text) <= self._limits.stream_limit
            and time.monotonic() - self._last_edit >= self._limits.edit_interval
        ):
            await self._show_draft()

    async def finish(self) -> bool:
        """Send the final version. Returns whether there was any text."""
        text = self._text.strip()
        if text:
            await self._send_final(split_text(text, self._limits.chunk))
        self._text, self._message_id = "", None
        return bool(text)

    async def _show_draft(self) -> None:
        draft = self._text.strip() + " …"
        if self._message_id is None:
            self._message_id = await self._chat.send(draft)
        else:
            await self._chat.edit(self._message_id, draft)
        self._last_edit = time.monotonic()

    async def _send_final(self, chunks: list[str]) -> None:
        first, *rest = chunks
        if self._message_id is None:
            await self._chat.send(first, markdown=True)
        else:
            await self._chat.edit(self._message_id, first, markdown=True)
        for chunk in rest:
            await self._chat.send(chunk, markdown=True)


def _describe(args: dict[str, Any]) -> str:
    text = str(next(iter(args.values()))) if len(args) == 1 else json.dumps(args, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "…"
