"""What the Slack channel sends lo9i from Slack: the owner's DMs, buttons pressed and /new and /home.
lo9i shows what follows in the DM through the stream's operations (channel.py). Only the owner (the
user who installed the app) is heard; everyone else is ignored."""

import html
from collections.abc import Sequence
from typing import Any, Protocol

import httpx
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.daemon import Attachment, DaemonError
from lo9i_slack.media import UnsupportedMediaError, attachments_from
from lo9i_slack.port import SlackChat

# A plain message, or one with files. Edits, deletions and bot messages have other subtypes.
_MESSAGE_SUBTYPES = {None, "file_share"}


class Conversation(Protocol):
    """The Daemon calls the handlers need."""

    async def message(self, chat: str, text: str, files: Sequence[Attachment] = (), voice: bool = False) -> None: ...
    async def press(self, chat: str, message: str, text: str, data: str) -> None: ...
    async def command(self, command: str) -> str: ...


class Handlers:
    def __init__(self, daemon: Conversation, web: AsyncWebClient, bot_token: str, owner_id: str) -> None:
        self._daemon = daemon
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
        try:
            await self._daemon.message(event["channel"], html.unescape(event.get("text", "")), attachments)
        except DaemonError as e:
            await chat.send(f"⚠️ {e}")

    async def button(self, payload: dict[str, Any]) -> None:
        if payload.get("type") != "block_actions" or payload.get("user", {}).get("id") != self._owner_id:
            return
        channel, message = payload["channel"]["id"], payload["message"]
        try:
            await self._daemon.press(channel, message["ts"], message.get("text", ""), payload["actions"][0]["value"])
        except DaemonError as e:
            await SlackChat(self._web, channel).send(f"⚠️ {e}")

    async def command(self, payload: dict[str, Any]) -> dict[str, str]:
        """Slash commands. lo9i's answer is sent with the acknowledgement, visible only to the owner."""
        if payload.get("user_id") != self._owner_id:
            return {"text": "This assistant only answers the person who set it up."}
        try:
            return {"text": await self._daemon.command(str(payload.get("command", "")).removeprefix("/"))}
        except DaemonError as e:
            return {"text": f"⚠️ {e}"}

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
