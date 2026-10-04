"""Workspace calls Slack as the user: channel names resolve to ids, results become short lines."""

from datetime import UTC
from unittest.mock import MagicMock, create_autospec

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.connection import SlackError
from lo9i_slack.workspace import Workspace

_PAGES = [
    {"channels": [{"id": "C1", "name": "random"}], "response_metadata": {"next_cursor": "p2"}},
    {"channels": [{"id": "C2", "name": "general"}], "response_metadata": {"next_cursor": ""}},
]


def _client() -> MagicMock:
    client = create_autospec(AsyncWebClient, instance=True)
    client.conversations_list.side_effect = lambda **kwargs: _PAGES[0 if kwargs["cursor"] is None else 1]
    client.users_info.return_value = {"user": {"real_name": "Ana Ruiz"}}
    client.conversations_history.return_value = {
        "messages": [
            {"user": "U1", "ts": "1700000060.0", "text": "second", "reply_count": 2},
            {"user": "U1", "ts": "1700000000.0", "text": "first"},
        ]
    }
    match = {
        "channel": {"name": "general"},
        "user": "U1",
        "ts": "1700000000.0",
        "text": "budget",
        "permalink": "https://l",
    }
    client.search_messages.return_value = {"messages": {"matches": [match]}}
    client.chat_postMessage.return_value = {"ts": "1.0"}
    client.chat_getPermalink.return_value = {"permalink": "https://acme.slack.com/C2/1.0"}
    return client


async def test_history_is_oldest_first_with_names_and_reply_counts():
    lines = await Workspace(_client(), UTC).history("#general", None, 30)
    assert lines[0] == "Ana Ruiz · 2023-11-14 22:13 · ts=1700000000.0\nfirst"
    assert lines[1].startswith("Ana Ruiz · 2023-11-14 22:14 · ts=1700000060.0 · 2 replies")


async def test_search_and_post_resolve_names_across_pages():
    client = _client()
    workspace = Workspace(client, UTC)
    assert (await workspace.search("budget", 20))[0].endswith("budget\nhttps://l")
    assert await workspace.post("general", "Ship it", "1.2") == "https://acme.slack.com/C2/1.0"
    assert client.chat_postMessage.call_args.kwargs == {"channel": "C2", "text": "Ship it", "thread_ts": "1.2"}


async def test_unknown_channels_and_slack_errors_are_slack_errors():
    client = _client()
    workspace = Workspace(client, UTC)
    with pytest.raises(SlackError, match="No channel named #nope"):
        await workspace.history("#nope", None, 30)
    client.chat_postMessage.side_effect = SlackApiError("bad", {"error": "channel_not_found"})
    with pytest.raises(SlackError, match="channel_not_found"):
        await workspace.post("C404", "hi", None)
