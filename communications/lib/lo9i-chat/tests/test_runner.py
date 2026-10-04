"""The runner hands stream events to the channel, acknowledges deliveries with their result, and
reconnects while the daemon is away."""

import asyncio

import pytest

from lo9i_chat.backoff import Backoff
from lo9i_chat.errors import DaemonUnavailableError, RefusedError
from lo9i_chat.runner import Runner


class FakeClient:
    """Each stream is a list of events, or an exception to raise when it opens. A stream ends once
    `acks` deliveries were acknowledged, so the runner's delivery tasks finish first."""

    def __init__(self, *streams, acks=0):
        self._streams = list(streams)
        self._expected = acks
        self.acks: list[tuple[str, str]] = []
        self._all_acked = asyncio.Event()

    async def stream(self):
        events = self._streams.pop(0) if self._streams else RefusedError(403, "stop")
        if isinstance(events, Exception):
            raise events
        for event in events:
            yield event
        if self._expected:
            await self._all_acked.wait()

    async def ack(self, delivery_id, error=""):
        self.acks.append((delivery_id, error))
        if len(self.acks) == self._expected:
            self._all_acked.set()


class FakeChannel:
    def __init__(self):
        self.calls = []

    async def hello(self, assistant_name):
        self.calls.append(("hello", assistant_name))

    async def renamed(self, assistant_name):
        self.calls.append(("renamed", assistant_name))

    async def deliver(self, text):
        if text == "fail":
            raise RuntimeError("No Telegram account is paired.")
        self.calls.append(("deliver", text))

    async def approval(self, interrupt_id, title, request):
        self.calls.append(("approval", interrupt_id, title))

    async def action(self, action):
        self.calls.append(("action", action))


async def _run(client, channel):
    """Runs until the fake client runs out of streams and refuses the next one."""
    runner = Runner(client, channel, Backoff(first=0))
    with pytest.raises(RefusedError):
        await runner.run()


async def test_events_reach_the_channel_and_deliveries_are_acknowledged():
    client = FakeClient(
        [
            ("hello", {"assistant_name": "Max"}),
            ("deliver", {"id": "d1", "text": "Stretch!"}),
            ("deliver", {"id": "d2", "text": "fail"}),
            ("approval", {"id": "d3", "interrupt_id": "i1", "title": "The job x", "request": {}}),
            ("renamed", {"assistant_name": "Ada"}),
            ("action", {"action": "new-pairing-code"}),
            ("future", {}),
        ],
        acks=3,
    )
    channel = FakeChannel()
    await _run(client, channel)
    assert ("hello", "Max") in channel.calls and ("renamed", "Ada") in channel.calls
    assert ("action", "new-pairing-code") in channel.calls and ("approval", "i1", "The job x") in channel.calls
    assert sorted(client.acks) == [("d1", ""), ("d2", "No Telegram account is paired."), ("d3", "")]


async def test_reconnects_while_the_daemon_is_away():
    client = FakeClient(DaemonUnavailableError("down"), [("hello", {"assistant_name": "Max"})], [])
    channel = FakeChannel()
    await _run(client, channel)
    assert channel.calls == [("hello", "Max")]


def test_the_backoff_grows_to_its_most():
    backoff = Backoff(first=1, most=30, attempts=3)
    assert [backoff.delay(n) for n in (1, 2, 3, 6, 10)] == [1, 2, 4, 30, 30]
    assert not backoff.gives_up(2) and backoff.gives_up(3)
