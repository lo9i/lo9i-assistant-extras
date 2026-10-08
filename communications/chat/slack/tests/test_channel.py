"""lo9i's chat operations in Slack: the user's own chat is the owner's DM, buttons carry lo9i's data,
and what Slack refuses fails the operation with Slack's reason."""

from unittest.mock import MagicMock, create_autospec

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.channel import SlackChannel
from lo9i_slack.connection import Owner
from lo9i_slack.runner import OperationError


class _Status:
    async def set_status(self, items):
        pass


def _web() -> MagicMock:
    web = create_autospec(AsyncWebClient, instance=True)
    web.conversations_open.return_value = {"channel": {"id": "D1"}}
    web.chat_postMessage.return_value = {"ts": "1.0"}
    return web


async def test_the_users_own_chat_is_the_owners_dm_and_buttons_carry_lo9is_data():
    web = _web()
    channel = SlackChannel(_Status(), web, Owner("Acme", "U1", "ani"))
    buttons = [["✅ Approve", "ja:i1"], ["❌ Reject", "jr:i1"]]
    assert await channel.operate("send", {"chat": "", "text": "The job is waiting", "buttons": buttons}) == "1.0"
    sent = web.chat_postMessage.call_args.kwargs
    assert web.conversations_open.call_args.kwargs["users"] == "U1" and sent["channel"] == "D1"
    assert [b["value"] for b in sent["blocks"][-1]["elements"]] == ["ja:i1", "jr:i1"]


async def test_an_edit_without_buttons_removes_them():
    web = _web()
    channel = SlackChannel(_Status(), web, Owner("Acme", "U1", "ani"))
    await channel.operate("edit", {"chat": "D1", "message": "5.0", "text": "Approve bash?", "buttons": []})
    assert web.chat_update.call_args.kwargs == {"channel": "D1", "ts": "5.0", "text": "Approve bash?", "blocks": []}
    assert await channel.operate("typing", {"chat": "D1"}) == ""


async def test_what_slack_refuses_fails_the_operation():
    web = _web()
    web.conversations_open.side_effect = SlackApiError("bad", {"error": "user_not_found"})
    channel = SlackChannel(_Status(), web, Owner("Acme", "U1", "ani"))
    with pytest.raises(OperationError, match="user_not_found"):
        await channel.operate("send", {"chat": "", "text": "hi"})
    with pytest.raises(OperationError, match="voice"):
        await channel.operate("voice", {"chat": "D1", "audio": ""})
