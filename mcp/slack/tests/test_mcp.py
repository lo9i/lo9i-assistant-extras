"""The workspace tools over MCP: search and read are read-only, posting isn't (lo9i asks the user)."""

import asyncio
from datetime import UTC

import pytest
from mcp import Client

import lo9i_slack.mcp_server as server
from lo9i_slack.connection import SlackError


class FakeWorkspace:
    async def search(self, query, count):
        return [f"#general · Ana · {query}"]

    async def history(self, channel, thread, limit):
        return []

    async def post(self, channel, text, thread):
        raise SlackError("Slack said: channel_not_found")


def call(*calls):
    async def run():
        async with Client(server.mcp) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return asyncio.run(run())


@pytest.fixture
def workspace(monkeypatch):
    monkeypatch.setattr(server, "_workspace", lambda: FakeWorkspace())


def test_tools_and_their_hints():
    async def run():
        async with Client(server.mcp) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(run())}
    assert set(tools) == {"slack_search", "slack_read", "slack_post"}
    assert tools["slack_search"].annotations.read_only_hint and tools["slack_read"].annotations.read_only_hint
    assert not (tools["slack_post"].annotations and tools["slack_post"].annotations.read_only_hint)


def test_results_and_slack_errors_reach_the_model(workspace):
    search, read, post = call(
        ("slack_search", {"query": "budget"}),
        ("slack_read", {"channel": "#general"}),
        ("slack_post", {"channel": "#nope", "text": "hi"}),
    )
    assert search.content[0].text == "#general · Ana · budget"
    assert read.content[0].text == "No messages."
    assert post.content[0].text == "Slack said: channel_not_found"


def test_without_a_user_token_the_tools_say_so(monkeypatch):
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    (result,) = call(("slack_search", {"query": "x"}))
    assert "isn't set up" in result.content[0].text


def test_times_use_tz(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    assert server._zone().utcoffset(None) == UTC.utcoffset(None)
