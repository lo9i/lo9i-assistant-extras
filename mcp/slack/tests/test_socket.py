"""Socket Mode: every Slack request is acknowledged before it's worked on, retries are skipped, and a
connection that can't open is an error."""

import asyncio

import pytest
from slack_sdk.errors import SlackClientError
from slack_sdk.socket_mode.request import SocketModeRequest

import lo9i_slack.socket_mode as socket_mode
from lo9i_slack.socket_mode import Requests


class FakeHandlers:
    def __init__(self):
        self.messages, self.buttons = [], []

    async def command(self, payload):
        return {"text": "Started a new conversation."}

    async def message(self, event):
        self.messages.append(event)

    async def button(self, payload):
        self.buttons.append(payload)


class FakeSocket:
    def __init__(self, app_token, web_client):
        self.app_token = app_token
        self.socket_mode_request_listeners = []
        self.responses = []
        self.closed = False

    async def connect(self):
        if self.app_token == "xapp-down":
            raise SlackClientError("down")

    async def close(self):
        self.closed = True

    async def send_socket_mode_response(self, response):
        self.responses.append(response)


async def test_requests_are_acknowledged_and_retries_skipped():
    handlers, socket = FakeHandlers(), FakeSocket("xapp-1", None)
    requests = Requests(handlers)  # type: ignore[arg-type]
    await requests.on_request(socket, SocketModeRequest("slash_commands", "e1", {"user_id": "U1", "command": "/new"}))
    assert socket.responses[0].payload == {"text": "Started a new conversation."}
    retry = SocketModeRequest("events_api", "e2", {"event": {"type": "message"}}, retry_attempt=1)
    await requests.on_request(socket, retry)
    await requests.on_request(socket, SocketModeRequest("interactive", "e3", {"type": "block_actions"}))
    assert [r.envelope_id for r in socket.responses] == ["e1", "e2", "e3"]
    await asyncio.sleep(0)  # let the spawned task run
    assert handlers.messages == [] and handlers.buttons == [{"type": "block_actions"}]


async def test_a_connection_that_cant_open_raises_and_closes(monkeypatch):
    sockets = []
    monkeypatch.setattr(
        socket_mode, "SocketModeClient", lambda *a, **k: sockets.append(FakeSocket(*a, **k)) or sockets[-1]
    )
    with pytest.raises(SlackClientError):
        async with socket_mode.connected("xapp-down", None, Requests(FakeHandlers())):  # type: ignore[arg-type]
            pass
    assert sockets[0].closed
