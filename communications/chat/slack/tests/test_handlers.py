"""The channel hears only the owner's DMs and buttons and sends them to lo9i; /new and /home are
answered with lo9i's text, privately."""

from unittest.mock import create_autospec

from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.daemon import DaemonUnavailableError
from lo9i_slack.handlers import Handlers


class FakeDaemon:
    def __init__(self):
        self.messages, self.presses, self.commands = [], [], []
        self.down = False

    async def message(self, chat, text, files=(), voice=False):
        self.messages.append((chat, text, files))

    async def press(self, chat, message, text, data):
        self.presses.append((chat, message, text, data))

    async def command(self, command, text=""):
        if self.down:
            raise DaemonUnavailableError("The assistant isn't reachable")
        self.commands.append((command, text))
        return "Started a side conversation. /home goes back."


def _handlers():
    web = create_autospec(AsyncWebClient, instance=True)
    web.chat_postMessage.return_value = {"ts": "9.0"}
    daemon = FakeDaemon()
    return Handlers(daemon, web, "xoxb-1", "U1"), daemon, web


def _dm(**fields):
    return {"type": "message", "channel_type": "im", "channel": "D1", "user": "U1", "text": "a &lt; b", **fields}


def _press(value, user="U1", text="Which sources?"):
    return {
        "type": "block_actions",
        "user": {"id": user},
        "actions": [{"value": value}],
        "channel": {"id": "D1"},
        "message": {"ts": "5.0", "text": text},
    }


async def test_the_owners_dm_goes_to_lo9i():
    handlers, daemon, _ = _handlers()
    await handlers.message(_dm())
    assert daemon.messages == [("D1", "a < b", [])]


async def test_others_edits_and_bots_are_ignored():
    handlers, daemon, _ = _handlers()
    for event in (_dm(user="U2"), _dm(subtype="message_changed"), _dm(bot_id="B1"), _dm(channel_type="channel")):
        await handlers.message(event)
    assert daemon.messages == []


async def test_video_is_refused():
    handlers, daemon, web = _handlers()
    await handlers.message(_dm(files=[{"id": "F1", "mimetype": "video/mp4", "url_private_download": "https://x"}]))
    assert daemon.messages == [] and "Video" in web.chat_postMessage.call_args.kwargs["text"]


async def test_a_button_sends_the_pressed_message_and_its_data():
    handlers, daemon, _ = _handlers()
    await handlers.button(_press("r:abc", text="Approve bash?"))
    await handlers.button(_press("r:abc", user="U2"))
    assert daemon.presses == [("D1", "5.0", "Approve bash?", "r:abc")]


async def test_commands_are_for_the_owner_only_and_answered_with_lo9is_text():
    handlers, daemon, _ = _handlers()
    started = "Started a side conversation. /home goes back."
    assert (await handlers.command({"user_id": "U1", "command": "/new"}))["text"] == started
    assert "only answers" in (await handlers.command({"user_id": "U2", "command": "/new"}))["text"]
    await handlers.command({"user_id": "U1", "command": "/claude", "text": " ~/code/app "})
    assert daemon.commands == [("new", ""), ("claude", "~/code/app")]
    daemon.down = True
    assert "isn't reachable" in (await handlers.command({"user_id": "U1", "command": "/home"}))["text"]
