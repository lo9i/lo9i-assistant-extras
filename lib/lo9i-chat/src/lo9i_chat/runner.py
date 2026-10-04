"""Keeps the channel's stream from the daemon open for as long as the process runs, and hands its
events to the channel. While the daemon is away it reconnects, waiting longer after each failure."""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, Protocol

from lo9i_chat.backoff import Backoff
from lo9i_chat.client import ChannelClient
from lo9i_chat.errors import DaemonError, DaemonUnavailableError

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """A channel couldn't deliver a message or a request; the text tells the assistant why."""


class Channel(Protocol):
    """What a chat channel does with the daemon's stream events."""

    async def hello(self, assistant_name: str) -> None:
        """The stream opened: the first event of every stream. A good time to send the status."""
        ...

    async def renamed(self, assistant_name: str) -> None: ...

    async def deliver(self, text: str) -> None:
        """Sends a message the assistant started (send_message). Raises when it can't."""
        ...

    async def approval(self, interrupt_id: str, title: str, request: dict[str, Any]) -> None:
        """Shows a scheduled job's request for approval with `ja:`/`jr:` buttons. Raises when it can't."""
        ...

    async def action(self, action: str) -> None:
        """The user pressed one of the channel's setup buttons in an app."""
        ...


class Runner:
    def __init__(self, client: ChannelClient, channel: Channel, backoff: Backoff | None = None) -> None:
        self._client = client
        self._channel = channel
        self._backoff = backoff or Backoff()
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        """Runs until cancelled. Raises RefusedError when the daemon refuses the stream (a token that
        isn't valid any more), which reconnecting can't fix."""
        failures = 0
        try:
            while True:
                failures = await self._read(failures) + 1
                logger.info("The stream from the assistant closed; reconnecting.")
                await asyncio.sleep(self._backoff.delay(failures))
        finally:
            for task in self._tasks:
                task.cancel()

    async def _read(self, failures: int) -> int:
        """Reads one stream until it ends. Returns the failures in a row: none once an event came."""
        try:
            async for kind, data in self._client.stream():
                failures = 0
                await self._dispatch(kind, data)
        except DaemonUnavailableError as e:
            logger.warning("%s", e)
        return failures

    async def _dispatch(self, kind: str, data: dict[str, Any]) -> None:
        """Deliveries and actions run on their own, so a slow one doesn't hold up the stream."""
        match kind:
            case "hello":
                await self._logged(self._channel.hello(data["assistant_name"]))
            case "renamed":
                await self._logged(self._channel.renamed(data["assistant_name"]))
            case "deliver":
                self._spawn(self._deliver(data["id"], self._channel.deliver(data["text"])))
            case "approval":
                work = self._channel.approval(data["interrupt_id"], data["title"], data["request"])
                self._spawn(self._deliver(data["id"], work))
            case "action":
                self._spawn(self._logged(self._channel.action(data["action"])))
            case _:
                logger.debug("Ignored a stream event of kind %r", kind)

    async def _deliver(self, delivery_id: str, work: Coroutine[Any, Any, None]) -> None:
        """Whatever the channel raises is the delivery's error, so the assistant reports why it failed."""
        try:
            await work
            error = ""
        except Exception as e:
            logger.warning("Delivery %s failed: %s", delivery_id, e)
            error = str(e) or type(e).__name__
        try:
            await self._client.ack(delivery_id, error)
        except DaemonError as e:
            logger.warning("Couldn't report delivery %s: %s", delivery_id, e)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _logged(work: Coroutine[Any, Any, None]) -> None:
        try:
            await work
        except Exception:
            logger.exception("The channel failed to handle a stream event")
