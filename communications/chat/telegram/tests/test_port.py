"""TelegramChat calls the real Bot API: an autospec of telegram.Bot checks every call's arguments."""

from telegram.constants import ParseMode

from lo9i_telegram.formatting import to_markdown_v2
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


async def test_an_edit_redraws_or_removes_the_buttons(bot):
    chat = TelegramChat(bot, 42)
    await chat.edit(7, "Which?", buttons=[["☑ X", "qt:q1:4:2"], ["✅ Done", "qd:q1:4"]])
    assert bot.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "☑ X"
    await chat.edit(7, "Which?")
    assert bot.edit_message_text.call_args.kwargs["reply_markup"] is None


def test_markdown_v2_escapes():
    assert to_markdown_v2("**bold** 1.5 `code`") == "*bold* 1\\.5 `code`"


def test_options_go_one_per_row_and_approvals_side_by_side():
    rows = markup([["☐ Google", "qt:q1:0:0"], ["☐ GitHub", "qt:q1:0:1"], ["✅ Done", "qd:q1:0"]])
    assert rows is not None and [len(r) for r in rows.inline_keyboard] == [1, 1, 1]
    pair = markup([["✅ Approve", "a:i1"], ["❌ Reject", "r:i1"]])
    assert pair is not None and [len(r) for r in pair.inline_keyboard] == [2]
