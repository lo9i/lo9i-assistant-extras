"""What the user does in Telegram goes to lo9i: messages with their files, buttons with the pressed
message, commands with lo9i's answer. Only paired accounts are heard."""

import json
from unittest.mock import AsyncMock, MagicMock, create_autospec

from telegram import CallbackQuery, Message, Update
from telegram.ext import ContextTypes

from lo9i_telegram.handlers import Handlers
from lo9i_telegram.pairing import FILE, Pairing


class FakeDaemon:
    def __init__(self):
        self.messages, self.presses, self.commands = [], [], []

    async def message(self, chat, text, files=(), voice=False):
        self.messages.append((chat, text, files, voice))

    async def press(self, chat, message, text, data):
        self.presses.append((chat, message, text, data))

    async def command(self, command, text=""):
        self.commands.append((command, text))
        return "Back home."


async def _handlers(tmp_path, daemon):
    (tmp_path / FILE).write_text(json.dumps({"users": [{"id": 222, "name": "Ani"}]}))
    return Handlers(daemon, await Pairing.load(tmp_path))


def _update(user_id=222, **fields) -> MagicMock:
    update = MagicMock(spec=Update, **fields)
    update.effective_user.id = user_id
    return update


def _context(bot):
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot = bot
    return context


async def test_a_button_sends_lo9i_the_pressed_message_and_its_data(tmp_path, bot):
    daemon = FakeDaemon()
    query = create_autospec(CallbackQuery, instance=True, data="a:i1")
    query.message = create_autospec(Message, instance=True, chat_id=222, message_id=9, text="Approve bash?")
    await (await _handlers(tmp_path, daemon)).button(_update(callback_query=query), _context(bot))
    assert daemon.presses == [("222", "9", "Approve bash?", "a:i1")] and query.answer.await_count == 1


async def test_strangers_are_not_heard(tmp_path, bot):
    daemon = FakeDaemon()
    query = create_autospec(CallbackQuery, instance=True, data="a:i1")
    query.message = create_autospec(Message, instance=True, chat_id=666, message_id=9, text="x")
    await (await _handlers(tmp_path, daemon)).button(_update(666, callback_query=query), _context(bot))
    assert daemon.presses == [] and query.answer.await_count == 1


async def test_a_command_is_answered_with_lo9is_text(tmp_path, bot):
    daemon = FakeDaemon()
    handlers = await _handlers(tmp_path, daemon)
    message = create_autospec(Message, instance=True, text="/home@lo9i_bot")
    await handlers.command(_update(effective_message=message), _context(bot))
    agent = create_autospec(Message, instance=True, text="/claude  ~/code/app ")
    await handlers.command(_update(effective_message=agent), _context(bot))
    assert daemon.commands == [("home", ""), ("claude", "~/code/app")]
    assert message.reply_text.call_args.args[0] == "Back home."


async def test_a_message_goes_to_lo9i_with_its_chat(tmp_path, bot):
    daemon = FakeDaemon()
    message = create_autospec(Message, instance=True, text="hi", caption=None, voice=None, audio=None, chat_id=222)
    message.video_note = message.photo = message.document = None
    message.reply_text = AsyncMock()
    await (await _handlers(tmp_path, daemon)).message(_update(effective_message=message), _context(bot))
    assert daemon.messages == [("222", "hi", [], False)]
