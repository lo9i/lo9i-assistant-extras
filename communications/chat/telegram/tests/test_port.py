"""TelegramChat calls the real Bot API: an autospec of telegram.Bot checks every call's arguments."""

from telegram.constants import ParseMode

from lo9i_chat.questions import question_buttons
from lo9i_telegram.formatting import CHUNK, to_markdown_v2
from lo9i_telegram.port import TelegramChat, markup


async def test_send_and_edit_with_markdown(bot):
    chat = TelegramChat(bot, 42)
    assert await chat.send("*hi*", markdown=True) == 7
    await chat.edit(7, "*bye*", markdown=True)
    assert bot.send_message.call_args.kwargs["parse_mode"] == ParseMode.MARKDOWN_V2
    edit = bot.edit_message_text.call_args
    assert edit.kwargs["chat_id"] == 42 and edit.kwargs["message_id"] == 7
    assert edit.kwargs["parse_mode"] == ParseMode.MARKDOWN_V2


async def test_plain_edit(bot):
    await TelegramChat(bot, 42).edit(7, "plain")
    assert bot.edit_message_text.call_args.args == ("plain",)


async def test_long_text_is_split_with_the_buttons_on_the_last_part(bot):
    command = "\n".join(f"echo line {i}" for i in range(800))  # a bash command over 4096 characters
    await TelegramChat(bot, 42).send(command, buttons=[("Approve", "a:1"), ("Reject", "r:1")])
    sent = bot.send_message.call_args_list
    assert len(sent) > 1 and all(len(c.args[1]) <= CHUNK for c in sent)
    assert "\n".join(c.args[1] for c in sent) == command
    assert [c.kwargs["reply_markup"] is not None for c in sent] == [False] * (len(sent) - 1) + [True]


def test_markdown_v2_escapes():
    assert to_markdown_v2("**bold** 1.5 `code`") == "*bold* 1\\.5 `code`"


def test_options_go_one_per_row_and_approvals_side_by_side():
    question = {"kind": "question", "question": "Which?", "options": ["Google", "GitHub", "X"], "multiple": True}
    rows = markup(question_buttons("q1", question))
    assert rows is not None and [len(r) for r in rows.inline_keyboard] == [1, 1, 1, 1]
    pair = markup([("✅ Approve", "a:i1"), ("❌ Reject", "r:i1")])
    assert pair is not None and [len(r) for r in pair.inline_keyboard] == [2]
