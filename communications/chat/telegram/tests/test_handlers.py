"""Buttons: a job's request is answered to the daemon, an approval resumes the run, a question's
toggle redraws its buttons; only paired accounts are heard."""

import json
from unittest.mock import AsyncMock, MagicMock, create_autospec

from telegram import CallbackQuery, Message, Update
from telegram.ext import ContextTypes

from lo9i_chat.events import ApprovalRequired, Done, TextDelta
from lo9i_telegram.handlers import Handlers
from lo9i_telegram.pairing import FILE, Pairing

_SOURCES = {"kind": "question", "question": "Which sources?", "options": ["Google", "GitHub", "X"], "multiple": True}
_SIDE = {"kind": "side_conversation", "title": "Taxes", "brief": "Do them.", "first_message": "Carried over: Taxes"}


class FakeClient:
    def __init__(self, waiting=True):
        self.answers, self.job_answers, self.messages, self.waiting = [], [], [], waiting
        self.moves: list[str] = []

    def message(self, text, files=()):
        self.messages.append((text, files))
        return self._events()

    def answer(self, decisions):
        self.answers.append(decisions)
        return self._events()

    async def _events(self):
        yield TextDelta("Done it.")
        yield Done()

    async def new_conversation(self):
        self.moves.append("new")
        return "t2"

    async def go_home(self):
        self.moves.append("home")
        return "home"

    async def pending_approvals(self):
        return [ApprovalRequired("q1", _SOURCES), ApprovalRequired("s1", _SIDE)]

    async def answer_job(self, interrupt_id, decision):
        self.job_answers.append((interrupt_id, decision))
        return self.waiting

    async def speech(self, text, after_voice_message):
        return None


async def _handlers(tmp_path, client):
    (tmp_path / FILE).write_text(json.dumps({"users": [{"id": 222, "name": "Ani"}]}))
    return Handlers(client, await Pairing.load(tmp_path))


def _press(data, user_id=222):
    query = create_autospec(CallbackQuery, instance=True, data=data)
    query.message = create_autospec(Message, instance=True)
    update = MagicMock(spec=Update, callback_query=query)
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    return update, query


def _context(bot):
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot = bot
    return context


async def test_a_job_button_answers_the_daemon(tmp_path, bot):
    client = FakeClient(waiting=False)
    handlers = await _handlers(tmp_path, client)
    update, query = _press("jr:i1")
    await handlers.button(update, _context(bot))
    assert client.job_answers == [("i1", {"approved": False, "reason": "rejected in Telegram"})]
    assert "already went on" in query.message.reply_text.call_args.args[0]


async def test_an_approval_button_resumes_the_run(tmp_path, bot):
    client = FakeClient()
    handlers = await _handlers(tmp_path, client)
    update, query = _press("a:i1")
    await handlers.button(update, _context(bot))
    assert client.answers == [{"i1": {"approved": True, "reason": ""}}]
    assert query.edit_message_reply_markup.call_args.kwargs == {"reply_markup": None}
    assert bot.send_message.call_args_list[0].args[1] == "Approved."


async def test_a_question_toggle_redraws_and_done_answers(tmp_path, bot):
    client = FakeClient()
    handlers = await _handlers(tmp_path, client)
    update, query = _press("qt:q1:0:2")
    await handlers.button(update, _context(bot))
    keyboard = query.edit_message_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in keyboard] == ["☐ Google", "☐ GitHub", "☑ X", "✅ Done"]
    assert client.answers == []
    update, _ = _press("qd:q1:5")
    await handlers.button(update, _context(bot))
    assert client.answers == [{"q1": {"choices": ["Google", "X"]}}]


async def test_moving_answers_then_continues_in_a_side_conversation(tmp_path, bot):
    client = FakeClient()
    handlers = await _handlers(tmp_path, client)
    update, _ = _press("sm:s1")
    await handlers.button(update, _context(bot))
    assert client.answers == [{"s1": {"moved": True}}]
    assert client.moves == ["new"] and client.messages == [("Carried over: Taxes", ())]
    update, _ = _press("sk:s1")
    await handlers.button(update, _context(bot))
    assert client.answers[-1] == {"s1": {"moved": False}} and client.moves == ["new"]


async def test_home_goes_back_to_the_home_conversation(tmp_path, bot):
    client = FakeClient()
    handlers = await _handlers(tmp_path, client)
    message = create_autospec(Message, instance=True)
    update = MagicMock(spec=Update, effective_message=message)
    update.effective_user.id = update.effective_chat.id = 222
    await handlers.home(update, _context(bot))
    assert client.moves == ["home"] and message.reply_text.call_args.args[0] == "Back home."


async def test_strangers_are_not_heard(tmp_path, bot):
    client = FakeClient()
    handlers = await _handlers(tmp_path, client)
    update, query = _press("a:i1", user_id=666)
    await handlers.button(update, _context(bot))
    assert client.answers == [] and query.answer.await_count == 1


async def test_a_message_runs_a_turn(tmp_path, bot):
    client = FakeClient()
    handlers = await _handlers(tmp_path, client)
    message = create_autospec(Message, instance=True, text="hi", caption=None, voice=None, audio=None)
    message.video_note = message.photo = message.document = None
    update = MagicMock(spec=Update, effective_message=message)
    update.effective_user.id = update.effective_chat.id = 222
    message.reply_text = AsyncMock()
    await handlers.message(update, _context(bot))
    assert client.messages == [("hi", [])]
    assert bot.edit_message_text.call_args.args[0] == "Done it\\."  # the draft, then the final version as MarkdownV2
