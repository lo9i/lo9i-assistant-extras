"""The Socket Mode connection to Slack: Slack's client reconnects by itself once it's open. Every
request is acknowledged right away (Slack resends after 3 seconds) and handled after."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Coroutine
from typing import Any

from slack_sdk.errors import SlackClientError
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.handlers import Handlers

logger = logging.getLogger(__name__)


class Requests:
    """Hands Slack's requests to the handlers, each in its own task."""

    def __init__(self, handlers: Handlers) -> None:
        self._handlers = handlers
        self._tasks: set[asyncio.Task[None]] = set()

    async def on_request(self, client: AsyncBaseSocketModeClient, request: SocketModeRequest) -> None:
        if request.type == "slash_commands":
            reply = await self._handlers.command(request.payload)
            await client.send_socket_mode_response(SocketModeResponse(request.envelope_id, payload=reply))
            return
        await client.send_socket_mode_response(SocketModeResponse(request.envelope_id))
        if request.retry_attempt:
            return
        if request.type == "events_api":
            self._spawn(self._handlers.message(request.payload.get("event", {})))
        elif request.type == "interactive":
            self._spawn(self._handlers.button(request.payload))

    def cancel(self) -> None:
        for task in self._tasks:
            task.cancel()

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(_logged(work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


@contextlib.asynccontextmanager
async def connected(app_token: str, web: AsyncWebClient, requests: Requests) -> AsyncIterator[None]:
    """Socket Mode is open inside the block. Raises when Slack refuses it or can't be reached."""
    socket = SocketModeClient(app_token, web_client=web)
    socket.socket_mode_request_listeners.append(requests.on_request)
    try:
        await socket.connect()
        logger.info("Slack connected.")
        yield
    finally:
        requests.cancel()
        with contextlib.suppress(SlackClientError, OSError):
            await socket.close()


async def _logged(work: Coroutine[Any, Any, None]) -> None:
    try:
        await work
    except Exception:
        logger.exception("Slack request failed")
