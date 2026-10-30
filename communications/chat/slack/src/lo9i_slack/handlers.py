"""What the Slack channel sends lo9i from Slack: the owner's DMs, with the message a thread reply answers,
buttons pressed and slash commands (/clear, an agent's /claude <folder>).
lo9i shows what follows in the DM through the stream's operations (channel.py). Only the owner (the
user who installed the app) is heard; everyone else is ignored."""

import html
import logging
from collections.abc import Sequence
from typing import Any, Protocol

import httpx
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.daemon import Attachment, DaemonError, DaemonUnavailableError
from lo9i_slack.i18n import Language
from lo9i_slack.media import UnsupportedMediaError, attachments_from
from lo9i_slack.port import SlackChat

logger = logging.getLogger(__name__)

# A plain message, or one with files. Edits, deletions and bot messages have other subtypes.
_MESSAGE_SUBTYPES = {None, "file_share"}


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


class Handlers:
    def __init__(self, daemon: Conversation, web: AsyncWebClient, bot_token: str, owner_id: str, t: Language) -> None:
        """`t`: the language the replies are in, which lo9i sets on the stream (channel.py)."""
        self._daemon = daemon
        self._web = web
        self._bot_token = bot_token
        self._owner_id = owner_id
        self._t = t

    async def message(self, event: dict[str, Any]) -> None:
        if not self._is_owner_dm(event):
            return
        chat = SlackChat(self._web, event["channel"])
        attachments = await self._attachments(chat, event.get("files", []))
        if attachments is None:
            return
        text = html.unescape(event.get("text", ""))
        try:
            await self._daemon.message(event["channel"], text, attachments, False, _replied(event), event.get("ts", ""))
        except DaemonError as e:
            await chat.send(self._failed(e))

    async def button(self, payload: dict[str, Any]) -> None:
        if payload.get("type") != "block_actions" or payload.get("user", {}).get("id") != self._owner_id:
            return
        channel, message = payload["channel"]["id"], payload["message"]
        # The text as it was sent (a reply's Markdown, which lo9i sends back to keep it when the buttons go).
        text = html.unescape(message.get("text", ""))
        try:
            await self._daemon.press(channel, message["ts"], text, payload["actions"][0]["value"])
        except DaemonError as e:
            await SlackChat(self._web, channel).send(self._failed(e))

    async def command(self, payload: dict[str, Any]) -> dict[str, str]:
        """Slash commands. lo9i's answer is sent with the acknowledgement, visible only to the owner."""
        if payload.get("user_id") != self._owner_id:
            return {"text": self._t("This assistant only answers the person who set it up.")}
        try:
            name, text = str(payload.get("command", "")).removeprefix("/"), str(payload.get("text", "")).strip()
            return {"text": await self._daemon.command(name, text)}
        except DaemonError as e:
            return {"text": self._failed(e)}

    async def _attachments(self, chat: SlackChat, files: list[dict[str, Any]]) -> list[Attachment] | None:
        """The message's files, or None after telling the owner why they can't be used."""
        try:
            return await attachments_from(files, self._bot_token)
        except UnsupportedMediaError as e:
            await chat.send(self._t(str(e)))
        except httpx.HTTPError as e:
            await chat.send(self._t("Couldn't download the file from Slack: {error}", error=e))
        return None

    def _failed(self, error: DaemonError) -> str:
        """What lo9i refused a request with is in the user's language already."""
        if isinstance(error, DaemonUnavailableError):
            logger.warning("%s", error)
            return self._t("⚠️ The assistant can't be reached right now. Try again in a moment.")
        return f"⚠️ {error}"

    def _is_owner_dm(self, event: dict[str, Any]) -> bool:
        return (
            event.get("type") == "message"
            and event.get("channel_type") == "im"
            and event.get("subtype") in _MESSAGE_SUBTYPES
            and not event.get("bot_id")
            and event.get("user") == self._owner_id
        )


def _replied(event: dict[str, Any]) -> str:
    """A reply in a thread answers the thread's first message, whose conversation it goes on in."""
    thread = str(event.get("thread_ts", ""))
    return thread if thread != event.get("ts") else ""
