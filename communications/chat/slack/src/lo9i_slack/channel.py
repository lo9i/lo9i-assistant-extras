"""What the Slack channel does with lo9i's stream (runner.py): keeps the apps' status current (the
manifest is named after the assistant) and carries out chat operations; the user's own chat is the
owner's DM with the bot."""

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.connection import Owner
from lo9i_slack.daemon import DaemonError
from lo9i_slack.port import SlackChat
from lo9i_slack.runner import Commands, OperationError
from lo9i_slack.status import items

logger = logging.getLogger(__name__)

# lo9i's chat for the user, for messages and requests the assistant starts.
_OWN_CHAT = ""


class StatusSink(Protocol):
    """The Daemon call the channel needs."""

    async def set_status(self, items: Sequence[dict[str, str]]) -> None: ...


class SlackChannel:
    def __init__(self, daemon: StatusSink, web: AsyncWebClient | None, connection: Owner | str) -> None:
        """Without `web` Slack refused the tokens, and `connection` says why."""
        self._daemon = daemon
        self._web = web
        self._connection = connection
        self._assistant_name = "lo9i"
        self._commands: Commands = []

    async def hello(self, assistant_name: str, commands: Commands) -> None:
        self._commands = commands
        await self.renamed(assistant_name)

    async def renamed(self, assistant_name: str) -> None:
        self._assistant_name = assistant_name or self._assistant_name
        await self._publish_status()

    async def commands(self, commands: Commands) -> None:
        """The manifest in the apps lists them; Slack offers them once the app is updated with it."""
        self._commands = commands
        await self._publish_status()

    async def action(self, action: str) -> None:
        logger.warning("Unknown action %r", action)

    async def operate(self, kind: str, data: dict[str, Any]) -> str:
        try:
            return await self._operate(kind, data)
        except (SlackApiError, SlackClientError, OSError) as e:
            raise OperationError(f"Slack didn't take it: {e}") from e

    async def _operate(self, kind: str, data: dict[str, Any]) -> str:
        chat = SlackChat(self._running(), await self._channel(data["chat"]))
        if kind == "send":
            return await chat.send(data["text"], markdown=data.get("markdown", False), buttons=data.get("buttons"))
        if kind == "edit":
            await chat.edit(
                data["message"], data["text"], markdown=data.get("markdown", False), buttons=data.get("buttons")
            )
        elif kind == "typing":
            await chat.typing()
        elif kind == "voice":
            raise OperationError("Slack can't take voice notes from the bot.")
        return ""

    async def _channel(self, chat: str) -> str:
        """The owner's DM for the user's own chat."""
        if chat != _OWN_CHAT:
            return chat
        if isinstance(self._connection, str):
            raise OperationError(self._connection)
        opened = (await self._running().conversations_open(users=self._connection.id)).get("channel") or {}
        return str(opened["id"])

    async def _publish_status(self) -> None:
        """While lo9i is away this fails quietly; every new stream starts with `hello`, which publishes it
        again."""
        try:
            await self._daemon.set_status(items(self._connection, self._assistant_name, self._commands))
        except DaemonError as e:
            logger.warning("Couldn't send the status: %s", e)

    def _running(self) -> AsyncWebClient:
        if self._web is None:
            raise OperationError(str(self._connection))
        return self._web
