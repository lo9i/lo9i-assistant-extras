"""What the Slack channel does with each request from Slack: DMs, buttons and /new. Only the owner
(the user who installed the app) is answered; everyone else is ignored."""

import html
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

import httpx
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_chat.approvals import parse_decision, parse_job_decision
from lo9i_chat.attachments import Attachment
from lo9i_chat.errors import DaemonError
from lo9i_chat.events import ApprovalRequired, Event, is_question
from lo9i_chat.questions import Press, answered_text, choices, parse_press, question_buttons, question_text
from lo9i_chat.turn import ChatTurn
from lo9i_slack.media import UnsupportedMediaError, attachments_from
from lo9i_slack.port import LIMITS, SlackChat

_PLACE = "Slack"
# A plain message, or one with files. Edits, deletions and bot messages have other subtypes.
_MESSAGE_SUBTYPES = {None, "file_share"}


class Conversation(Protocol):
    """The ChannelClient calls the handlers need."""

    def message(self, text: str, files: Sequence[Attachment] = ()) -> AsyncIterator[Event]: ...
    def answer(self, decisions: dict[str, Any]) -> AsyncIterator[Event]: ...
    async def new_conversation(self) -> str: ...
    async def pending_approvals(self) -> list[ApprovalRequired]: ...
    async def answer_job(self, interrupt_id: str, decision: dict[str, Any]) -> bool: ...


class Handlers:
    def __init__(self, client: Conversation, web: AsyncWebClient, bot_token: str, owner_id: str) -> None:
        self._client = client
        self._web = web
        self._bot_token = bot_token
        self._owner_id = owner_id

    async def message(self, event: dict[str, Any]) -> None:
        if not self._is_owner_dm(event):
            return
        chat = SlackChat(self._web, event["channel"])
        attachments = await self._attachments(chat, event.get("files", []))
        if attachments is None:
            return
        text = html.unescape(event.get("text", ""))
        await ChatTurn(chat, LIMITS).render(self._client.message(text, attachments))

    async def button(self, payload: dict[str, Any]) -> None:
        if payload.get("type") != "block_actions" or payload.get("user", {}).get("id") != self._owner_id:
            return
        chat = SlackChat(self._web, payload["channel"]["id"])
        try:
            await self._press(payload, payload["actions"][0].get("value", ""), chat)
        except DaemonError as e:
            await chat.send(f"⚠️ {e}")

    async def command(self, payload: dict[str, Any]) -> dict[str, str]:
        """Slash commands. The reply is sent with the acknowledgement, visible only to the owner."""
        if payload.get("user_id") != self._owner_id:
            return {"text": "This assistant only answers the person who set it up."}
        if payload.get("command") != "/new":
            return {"text": f"Unknown command {payload.get('command')}."}
        try:
            await self._client.new_conversation()
        except DaemonError as e:
            return {"text": f"⚠️ {e}"}
        return {"text": "Started a new conversation."}

    async def _press(self, payload: dict[str, Any], data: str, chat: SlackChat) -> None:
        if job_decision := parse_job_decision(data, _PLACE):
            await self._answer_job(payload, chat, *job_decision)
        elif press := parse_press(data):
            await self._answer_question(payload, chat, press)
        elif decision := parse_decision(data, _PLACE):
            await self._answer_approval(payload, chat, *decision)

    async def _answer_approval(
        self, payload: dict[str, Any], chat: SlackChat, interrupt_id: str, answer: dict[str, Any]
    ) -> None:
        await chat.edit(payload["message"]["ts"], payload["message"].get("text", ""))
        await chat.send("Approved." if answer["approved"] else "Rejected.")
        await ChatTurn(chat, LIMITS).render(self._client.answer({interrupt_id: answer}))

    async def _answer_question(self, payload: dict[str, Any], chat: SlackChat, press: Press) -> None:
        """A toggle only redraws the buttons; a pick or Done answers and the run goes on."""
        ts = payload["message"]["ts"]
        request = await self._waiting_question(press.interrupt_id)
        if request is None:
            await chat.edit(ts, payload["message"].get("text", ""))
            await chat.send("This question isn't waiting for an answer any more.")
            return
        if not press.answers:
            await chat.edit(
                ts, question_text(request), buttons=question_buttons(press.interrupt_id, request, press.picked)
            )
            return
        picked = choices(request, press.picked)
        await chat.edit(ts, question_text(request))
        await chat.send(answered_text(picked))
        await ChatTurn(chat, LIMITS).render(self._client.answer({press.interrupt_id: {"choices": picked}}))

    async def _waiting_question(self, interrupt_id: str) -> dict[str, Any] | None:
        waiting = await self._client.pending_approvals()
        return next((a.request for a in waiting if a.interrupt_id == interrupt_id and is_question(a.request)), None)

    async def _answer_job(
        self, payload: dict[str, Any], chat: SlackChat, interrupt_id: str, answer: dict[str, Any]
    ) -> None:
        """A scheduled job's request: the job's run continues on its own, nothing to show here."""
        await chat.edit(payload["message"]["ts"], payload["message"].get("text", ""))
        if not await self._client.answer_job(interrupt_id, answer):
            await chat.send("This request isn't waiting any more: the job's run already went on without it.")
        elif answer["approved"]:
            await chat.send("Approved. The job goes on.")
        else:
            await chat.send("Rejected. The job goes on without it.")

    async def _attachments(self, chat: SlackChat, files: list[dict[str, Any]]) -> list[Attachment] | None:
        """The message's files, or None after telling the owner why they can't be used."""
        try:
            return await attachments_from(files, self._bot_token)
        except UnsupportedMediaError as e:
            await chat.send(str(e))
        except httpx.HTTPError as e:
            await chat.send(f"Couldn't download the file from Slack: {e}")
        return None

    def _is_owner_dm(self, event: dict[str, Any]) -> bool:
        return (
            event.get("type") == "message"
            and event.get("channel_type") == "im"
            and event.get("subtype") in _MESSAGE_SUBTYPES
            and not event.get("bot_id")
            and event.get("user") == self._owner_id
        )
