"""Keeps the channel's stream from lo9i open for as long as the process runs, and hands its events to
the channel: lo9i's name, the apps' setup buttons, and the chat operations, each confirmed with its
result, and the commands the channel offers. While lo9i is away it reconnects, waiting longer after each failure."""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, Protocol

from lo9i_slack.daemon import Daemon, DaemonError, DaemonUnavailableError, Limits

logger = logging.getLogger(__name__)

_OPERATIONS = ("send", "edit", "typing", "voice")
_FIRST_DELAY, _MOST_DELAY = 1.0, 30.0


class OperationError(Exception):
    """The channel couldn't carry out an operation; the text tells lo9i why."""


# What the channel offers: {"command": "new", "description": "..."} each, /new, /home and each agent's.
Commands = list[dict[str, str]]


class Channel(Protocol):
    async def hello(self, assistant_name: str, commands: Commands) -> None:
        """The stream opened: the first event of every stream. A good time to send the status."""
        ...

    async def renamed(self, assistant_name: str) -> None: ...

    async def commands(self, commands: Commands) -> None:
        """The commands changed: an agent plugin was installed or removed."""
        ...

    async def action(self, action: str) -> None:
        """The user pressed one of the channel's setup buttons in an app."""
        ...

    async def operate(self, kind: str, data: dict[str, Any]) -> str:
        """Carries out a chat operation; returns the id of the message it sent ("" for others). Raises
        when it can't."""
        ...


class Runner:
    def __init__(self, daemon: Daemon, channel: Channel, limits: Limits) -> None:
        self._daemon = daemon
        self._channel = channel
        self._limits = limits
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        """Runs until cancelled. Raises RefusedError when lo9i refuses the stream (a token that isn't
        valid any more), which reconnecting can't fix."""
        failures = 0
        try:
            while True:
                failures = await self._read(failures) + 1
                logger.info("The stream from the assistant closed; reconnecting.")
                await asyncio.sleep(min(_FIRST_DELAY * 2 ** (failures - 1), _MOST_DELAY))
        finally:
            for task in self._tasks:
                task.cancel()

    async def _read(self, failures: int) -> int:
        """Reads one stream until it ends. Returns the failures in a row: none once an event came."""
        try:
            async for kind, data in self._daemon.stream(self._limits):
                failures = 0
                try:
                    await self._dispatch(kind, data)
                except (KeyError, TypeError, ValueError):
                    logger.exception("Ignored a malformed %s event from the assistant", kind)
        except DaemonUnavailableError as e:
            logger.warning("%s", e)
        except ValueError as e:
            logger.warning("The stream from the assistant isn't readable: %s", e)
        return failures

    async def _dispatch(self, kind: str, data: dict[str, Any]) -> None:
        """Operations and actions run on their own, so a slow one doesn't hold up the stream."""
        if kind in _OPERATIONS:
            self._spawn(self._operate(kind, data))
        elif kind == "hello":
            await _logged(self._channel.hello(data["assistant_name"], data.get("commands", [])))
        elif kind == "commands":
            await _logged(self._channel.commands(data["commands"]))
        elif kind == "renamed":
            await _logged(self._channel.renamed(data["assistant_name"]))
        elif kind == "action":
            self._spawn(_logged(self._channel.action(data["action"])))

    async def _operate(self, kind: str, data: dict[str, Any]) -> None:
        """Whatever the channel raises is the operation's error, so lo9i knows why it failed."""
        if not (operation := data.get("id")):
            logger.warning("Ignored a %s operation without an id", kind)
            return
        message, error = "", ""
        try:
            message = await self._channel.operate(kind, data)
        except Exception as e:
            logger.warning("%s failed: %s", kind, e)
            error = str(e) or type(e).__name__
        try:
            await self._daemon.done(operation, error, message)
        except DaemonError as e:
            logger.warning("Couldn't report %s: %s", kind, e)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def _logged(work: Coroutine[Any, Any, None]) -> None:
    try:
        await work
    except Exception:
        logger.exception("The channel failed to handle a stream event")
