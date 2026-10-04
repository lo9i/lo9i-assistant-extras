"""What the Slack channel does with the daemon's stream (lo9i_chat.runner.Channel): keeps the apps'
status current (the manifest is named after the assistant) and delivers messages and scheduled jobs'
requests to the owner's DM."""

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from lo9i_chat.errors import DaemonError
from lo9i_chat.runner import DeliveryError
from lo9i_chat.status import SetupItem
from lo9i_slack.connection import Owner
from lo9i_slack.sender import SlackSender
from lo9i_slack.status import items

logger = logging.getLogger(__name__)


class StatusSink(Protocol):
    """The ChannelClient call the channel needs."""

    async def set_status(self, items: Sequence[SetupItem]) -> None: ...


class SlackChannel:
    def __init__(self, client: StatusSink, sender: SlackSender | None, connection: Owner | str) -> None:
        """Without a sender Slack refused the tokens, and `connection` says why."""
        self._client = client
        self._sender = sender
        self._connection = connection
        self._assistant_name = "lo9i"

    async def hello(self, assistant_name: str) -> None:
        await self.renamed(assistant_name)

    async def renamed(self, assistant_name: str) -> None:
        self._assistant_name = assistant_name or self._assistant_name
        await self._publish_status()

    async def deliver(self, text: str) -> None:
        await self._running().send(text)

    async def approval(self, interrupt_id: str, title: str, request: dict[str, Any]) -> None:
        await self._running().forward(interrupt_id, title, request)

    async def action(self, action: str) -> None:
        logger.warning("Unknown action %r", action)

    async def _publish_status(self) -> None:
        """While the daemon is away this fails quietly; every new stream starts with `hello`, which
        publishes it again."""
        try:
            await self._client.set_status(items(self._connection, self._assistant_name))
        except DaemonError as e:
            logger.warning("Couldn't send the status: %s", e)

    def _running(self) -> SlackSender:
        if self._sender is None:
            raise DeliveryError(str(self._connection))
        return self._sender
