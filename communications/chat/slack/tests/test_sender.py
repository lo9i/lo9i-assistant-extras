"""Messages the assistant starts, and scheduled jobs' requests, go to the owner's DM with the bot."""

from unittest.mock import MagicMock, create_autospec

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_chat.runner import DeliveryError
from lo9i_slack.sender import SlackSender


def _client() -> MagicMock:
    client = create_autospec(AsyncWebClient, instance=True)
    client.conversations_open.return_value = {"channel": {"id": "D1"}}
    client.chat_postMessage.return_value = {"ts": "1.0"}
    return client


async def test_sends_to_the_owners_dm():
    client = _client()
    await SlackSender(client, "U1").send("Stretch!")
    assert client.conversations_open.call_args.kwargs["users"] == "U1"
    assert client.chat_postMessage.call_args.kwargs["channel"] == "D1"


async def test_a_slack_error_is_a_delivery_error():
    client = _client()
    client.conversations_open.side_effect = SlackApiError("bad", {"error": "user_not_found"})
    with pytest.raises(DeliveryError, match="user_not_found"):
        await SlackSender(client, "U1").send("hi")


async def test_job_requests_come_with_job_buttons():
    client = _client()
    request = {"tool": "bash", "command": "rm -rf build", "reason": "recursive delete"}
    await SlackSender(client, "U1").forward("i1", "The job cleanup", request)
    sent = client.chat_postMessage.call_args.kwargs
    assert sent["channel"] == "D1" and sent["text"].startswith("The job cleanup is waiting for you.")
    assert [b["value"] for b in sent["blocks"][-1]["elements"]] == ["ja:i1", "jr:i1"]
