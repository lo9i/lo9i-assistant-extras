"""The tokens are checked with Slack before the channel starts, and the status shows the outcome."""

import json

import pytest

import lo9i_slack.connection as connection
from lo9i_slack.channel import SlackChannel
from lo9i_slack.connection import Owner, SlackError, Tokens, verify
from lo9i_slack.runner import OperationError

_ANSWERS = {
    "xoxb-1": {"team_id": "T1", "team": "Acme", "user_id": "B1", "user": "bot"},
    "xoxp-1": {"team_id": "T1", "team": "Acme", "user_id": "U1", "user": "ani"},
    "xoxp-other": {"team_id": "T2", "team": "Other", "user_id": "U9", "user": "x"},
}


class FakeWebClient:
    """auth.test answers per token, as Slack would."""

    def __init__(self, token):
        self.token = token

    async def api_call(self, method):
        return _ANSWERS.get(self.token, {})


@pytest.fixture(autouse=True)
def fake_slack(monkeypatch):
    monkeypatch.setattr(connection, "AsyncWebClient", FakeWebClient)


async def test_the_owner_is_the_user_tokens_user():
    assert await verify(Tokens("xapp-1", "xoxb-1", "xoxp-1")) == Owner("Acme", "U1", "ani")


async def test_wrong_prefixes_and_mixed_workspaces_are_refused():
    with pytest.raises(SlackError, match="App token should start with xapp-"):
        await verify(Tokens("xoxb-1", "xoxb-1", "xoxp-1"))
    with pytest.raises(SlackError, match="different workspaces"):
        await verify(Tokens("xapp-1", "xoxb-1", "xoxp-other"))


def test_the_manifest_is_named_after_the_assistant():
    app = json.loads(connection.manifest("Max"))
    assert app["display_information"]["name"] == "Max" and app["features"]["bot_user"]["display_name"] == "Max"
    assert app["settings"]["socket_mode_enabled"] is True


class FakeClient:
    def __init__(self):
        self.statuses = []

    async def set_status(self, items):
        self.statuses.append([(i["kind"], i["label"], i["value"]) for i in items])


async def test_the_status_shows_the_workspace_and_a_manifest_that_follows_the_name():
    client = FakeClient()
    channel = SlackChannel(client, None, Owner("Acme", "U1", "ani"))
    await channel.hello("Max")
    await channel.renamed("Ada")
    (kind, _, line), (copy_kind, label, manifest) = client.statuses[-1]
    assert (kind, line) == ("text", "Workspace: Acme · owner: ani")
    assert (copy_kind, label) == ("copy", "App manifest") and json.loads(manifest)["display_information"][
        "name"
    ] == "Ada"


async def test_refused_tokens_show_why_and_fail_operations():
    client = FakeClient()
    channel = SlackChannel(client, None, "Slack rejected the bot token: invalid_auth")
    await channel.hello("Max")
    assert client.statuses[-1][0] == ("text", "", "Slack rejected the bot token: invalid_auth")
    with pytest.raises(OperationError, match="invalid_auth"):
        await channel.operate("send", {"chat": "", "text": "hi"})
