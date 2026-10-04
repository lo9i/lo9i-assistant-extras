"""The channel answers only the owner's DMs, turns buttons into approval decisions and job answers,
and handles /new."""

from unittest.mock import create_autospec

from slack_sdk.web.async_client import AsyncWebClient

from lo9i_chat.errors import DaemonUnavailableError
from lo9i_chat.events import ApprovalRequired, Done, TextDelta
from lo9i_slack.handlers import Handlers


class FakeClient:
    def __init__(self):
        self.sent, self.answers, self.job_answers, self.new_conversations = [], [], [], 0
        self.waiting = []
        self.down = False

    def message(self, text, files=()):
        self.sent.append((text, files))
        return self._events()

    def answer(self, decisions):
        self.answers.append(decisions)
        return self._events()

    async def _events(self):
        yield TextDelta("Hello")
        yield Done()

    async def new_conversation(self):
        if self.down:
            raise DaemonUnavailableError("The assistant isn't reachable")
        self.new_conversations += 1
        return "t2"

    async def pending_approvals(self):
        return self.waiting

    async def answer_job(self, interrupt_id, decision):
        self.job_answers.append((interrupt_id, decision))
        return True


def _handlers():
    web = create_autospec(AsyncWebClient, instance=True)
    web.chat_postMessage.return_value = {"ts": "9.0"}
    client = FakeClient()
    return Handlers(client, web, "xoxb-1", "U1"), client, web


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


async def test_the_owners_dm_runs_a_turn():
    handlers, client, web = _handlers()
    await handlers.message(_dm())
    assert client.sent == [("a < b", [])]
    # A draft first, then the final version as Markdown.
    assert web.chat_update.call_args.kwargs["blocks"] == [{"type": "markdown", "text": "Hello"}]


async def test_others_edits_and_bots_are_ignored():
    handlers, client, _ = _handlers()
    for event in (_dm(user="U2"), _dm(subtype="message_changed"), _dm(bot_id="B1"), _dm(channel_type="channel")):
        await handlers.message(event)
    assert client.sent == []


async def test_video_is_refused():
    handlers, client, web = _handlers()
    await handlers.message(_dm(files=[{"id": "F1", "mimetype": "video/mp4", "url_private_download": "https://x"}]))
    assert client.sent == [] and "Video" in web.chat_postMessage.call_args.kwargs["text"]


async def test_an_approval_button_resumes_the_run():
    handlers, client, web = _handlers()
    await handlers.button(_press("r:abc", text="Approve bash?"))
    assert client.answers == [{"abc": {"approved": False, "reason": "rejected in Slack"}}]
    assert web.chat_update.call_args_list[0].kwargs["blocks"] == []  # buttons removed
    await handlers.button(_press("r:abc", user="U2"))
    assert len(client.answers) == 1


async def test_a_job_button_answers_the_daemon():
    handlers, client, web = _handlers()
    await handlers.button(_press("ja:i1", text="The job x is waiting for you."))
    assert client.job_answers == [("i1", {"approved": True, "reason": ""})]
    assert web.chat_update.call_args.kwargs["blocks"] == []
    assert web.chat_postMessage.call_args.kwargs["text"] == "Approved. The job goes on."


async def test_new_starts_a_conversation_for_the_owner_only():
    handlers, client, _ = _handlers()
    assert (await handlers.command({"user_id": "U1", "command": "/new"}))["text"] == "Started a new conversation."
    assert "only answers" in (await handlers.command({"user_id": "U2", "command": "/new"}))["text"]
    assert client.new_conversations == 1
    client.down = True
    assert "isn't reachable" in (await handlers.command({"user_id": "U1", "command": "/new"}))["text"]


_SOURCES = {"kind": "question", "question": "Which sources?", "options": ["Google", "GitHub", "X"], "multiple": True}


async def test_question_toggles_redraw_the_buttons_and_done_answers():
    handlers, client, web = _handlers()
    client.waiting = [ApprovalRequired("q1", _SOURCES)]
    await handlers.button(_press("qt:q1:0:2"))
    labels = [e["text"]["text"] for e in web.chat_update.call_args.kwargs["blocks"][-1]["elements"]]
    assert labels == ["☐ Google", "☐ GitHub", "☑ X", "✅ Done"]
    assert client.answers == []
    await handlers.button(_press("qd:q1:5"))
    assert client.answers == [{"q1": {"choices": ["Google", "X"]}}]
    assert "Picked: Google, X." in web.chat_postMessage.call_args_list[0].kwargs["text"]


async def test_a_question_no_longer_waiting_is_said_so():
    handlers, client, web = _handlers()
    await handlers.button(_press("qp:q1:0"))
    assert client.answers == []
    assert "isn't waiting" in web.chat_postMessage.call_args.kwargs["text"]
