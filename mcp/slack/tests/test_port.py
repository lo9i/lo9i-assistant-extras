"""SlackChat calls the real Web API: an autospec of AsyncWebClient checks every call's arguments."""

from unittest.mock import MagicMock, create_autospec

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.port import SlackChat


def _client() -> MagicMock:
    client = create_autospec(AsyncWebClient, instance=True)
    client.chat_postMessage.return_value = {"ts": "1.5"}
    return client


async def test_markdown_goes_in_a_markdown_block():
    client = _client()
    assert await SlackChat(client, "D1").send("**hi**", markdown=True) == "1.5"
    sent = client.chat_postMessage.call_args.kwargs
    assert sent["channel"] == "D1" and sent["text"] == "**hi**"
    assert sent["blocks"] == [{"type": "markdown", "text": "**hi**"}]


async def test_buttons_carry_the_approval_data():
    client = _client()
    await SlackChat(client, "D1").send("Approve bash?", buttons=[("✅ Approve", "a:x"), ("❌ Reject", "r:x")])
    section, actions = client.chat_postMessage.call_args.kwargs["blocks"]
    assert section["text"]["text"] == "Approve bash?"
    assert [b["value"] for b in actions["elements"]] == ["a:x", "r:x"]


async def test_rejected_markdown_is_sent_and_edited_as_plain_text():
    client = _client()
    client.chat_postMessage.side_effect = [SlackApiError("bad", {"error": "invalid_blocks"}), {"ts": "2.0"}]
    assert await SlackChat(client, "D1").send("**hi**", markdown=True) == "2.0"
    assert "blocks" not in client.chat_postMessage.call_args.kwargs
    client.chat_update.side_effect = [SlackApiError("bad", {"error": "invalid_blocks"}), {}]
    await SlackChat(client, "D1").edit("2.0", "**hi**", markdown=True)
    assert client.chat_update.call_args.kwargs["blocks"] == []


async def test_plain_text_errors_are_not_hidden():
    client = _client()
    client.chat_postMessage.side_effect = SlackApiError("bad", {"error": "channel_not_found"})
    with pytest.raises(SlackApiError):
        await SlackChat(client, "D1").send("hi")
