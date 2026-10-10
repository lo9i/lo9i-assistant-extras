"""The channel's side of lo9i's protocol: the stream with the channel's limits, each operation carried out
and confirmed with its result, and what the user does sent to lo9i."""

import asyncio
import json

import httpx
import pytest

from lo9i_slack.daemon import Attachment, Daemon, DaemonUnavailableError, RefusedError
from lo9i_slack.port import LIMITS
from lo9i_slack.runner import OperationError, Runner


class FakeLo9i:
    """Answers with the queued handlers in order and keeps every request."""

    def __init__(self, *handlers):
        self.requests: list[httpx.Request] = []
        self._handlers = list(handlers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self._handlers.pop(0)(request) if self._handlers else httpx.Response(200, json={"ok": True})


def _daemon(lo9i: FakeLo9i) -> Daemon:
    return Daemon(httpx.AsyncClient(base_url="http://lo9i", transport=httpx.MockTransport(lo9i)), "slack")


def _sse(*events: tuple[str, dict]):
    text = "".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in events)
    return lambda request: httpx.Response(200, text=": ping\n\n" + text, headers={"content-type": "text/event-stream"})


async def test_the_stream_gives_lo9i_the_channels_limits():
    lo9i = FakeLo9i(_sse(("hello", {"assistant_name": "Max"})))
    events = [e async for e in _daemon(lo9i).stream(LIMITS)]
    assert events == [("hello", {"assistant_name": "Max"})]
    assert dict(lo9i.requests[0].url.params) == {
        "edit_seconds": "1.2",
        "draft_limit": "3500",
        "message_limit": "11000",
        "voice": "false",
    }


async def test_what_the_user_does_goes_to_lo9i():
    lo9i = FakeLo9i(
        lambda r: httpx.Response(200, json={"ok": True}),
        lambda r: httpx.Response(200, json={"ok": True}),
        lambda r: httpx.Response(200, json={"text": "Back home."}),
    )
    daemon = _daemon(lo9i)
    await daemon.message("42", "look", [Attachment("a.jpg", "image/jpeg", b"x")], voice=True)
    await daemon.press("42", "9", "Approve?", "a:i1")
    assert await daemon.command("claude", "~/code/app") == "Back home."
    sent = lo9i.requests[0].content
    assert b'name="chat"\r\n\r\n42' in sent and b'name="voice"\r\n\r\ntrue' in sent and b'filename="a.jpg"' in sent
    assert json.loads(lo9i.requests[1].content) == {"chat": "42", "message": "9", "text": "Approve?", "data": "a:i1"}
    assert json.loads(lo9i.requests[2].content) == {"command": "claude", "text": "~/code/app"}
    assert [r.url.path for r in lo9i.requests] == [
        "/channels/slack/messages",
        "/channels/slack/presses",
        "/channels/slack/commands",
    ]


async def test_refused_and_unreachable_are_told_apart():
    def down(request):
        raise httpx.ConnectError("refused")

    lo9i = FakeLo9i(lambda r: httpx.Response(403, json={"detail": "Only the slack channel"}), down)
    with pytest.raises(RefusedError, match="Only the slack channel"):
        await _daemon(lo9i).command("new")
    with pytest.raises(DaemonUnavailableError):
        await _daemon(lo9i).command("new")


class FakeChannel:
    def __init__(self):
        self.names, self.done = [], asyncio.Event()

    async def hello(self, assistant_name, commands):
        self.names.append(assistant_name)
        self.offered = commands

    async def commands(self, commands):
        self.offered = commands

    async def renamed(self, assistant_name):
        self.names.append(assistant_name)

    async def action(self, action):
        pass

    async def operate(self, kind, data):
        self.done.set()
        if data["chat"] == "":
            raise OperationError("Slack rejected the bot token.")
        return "1001"


async def test_each_operation_is_confirmed_with_its_result_or_its_error():
    sent = {"id": "op1", "chat": "42", "text": "hi"}
    broadcast = {"id": "op2", "chat": "", "text": "hi"}
    claude = [{"command": "claude", "description": "Hand this conversation to Claude Code"}]
    events = [("hello", {"assistant_name": "Max", "commands": []}), ("commands", {"commands": claude})]
    lo9i = FakeLo9i(_sse(*events, ("send", sent), ("send", broadcast)))
    channel = FakeChannel()
    running = asyncio.create_task(Runner(_daemon(lo9i), channel, LIMITS).run())
    for _ in range(100):
        if len([r for r in lo9i.requests if "deliveries" in r.url.path]) == 2:
            break
        await asyncio.sleep(0.01)
    running.cancel()
    results = {r.url.path.rsplit("/", 1)[1]: json.loads(r.content) for r in lo9i.requests if "deliveries" in r.url.path}
    assert results == {
        "op1": {"error": "", "message": "1001"},
        "op2": {"error": "Slack rejected the bot token.", "message": ""},
    }
    assert channel.names == ["Max"] and channel.offered == claude


async def test_a_malformed_event_is_skipped_and_the_stream_goes_on():
    events = [("hello", {}), ("send", {"chat": "42"}), ("renamed", {"assistant_name": "Max"})]
    lo9i = FakeLo9i(_sse(*events, ("send", {"id": "op1", "chat": "42", "text": "hi"})))
    channel = FakeChannel()
    running = asyncio.create_task(Runner(_daemon(lo9i), channel, LIMITS).run())
    for _ in range(100):
        if any("deliveries" in r.url.path for r in lo9i.requests):
            break
        await asyncio.sleep(0.01)
    running.cancel()
    assert channel.names == ["Max"]
    assert [r.url.path.rsplit("/", 1)[1] for r in lo9i.requests if "deliveries" in r.url.path] == ["op1"]
