"""The channel publishes what the apps show, opens pairing codes from the app, and carries out lo9i's
chat operations: in one chat, or for the user's own chat, in every paired account."""

import json

import pytest
from telegram.error import NetworkError

from lo9i_telegram.channel import TelegramChannel
from lo9i_telegram.pairing import FILE, Pairing
from lo9i_telegram.runner import OperationError
from lo9i_telegram.status import NEW_PAIRING_CODE


class FakeClient:
    def __init__(self):
        self.statuses = []

    async def set_status(self, items):
        self.statuses.append([(i["kind"], i["label"], i["value"]) for i in items])


async def test_hello_names_the_bot_and_shows_the_open_code(tmp_path, bot):
    client, pairing = FakeClient(), await Pairing.load(tmp_path)
    await TelegramChannel(client, pairing, bot, "Bot: @lo9i_bot").hello("Max")
    assert bot.set_my_name.call_args.args == ("Max",)
    assert client.statuses[-1] == [
        ("text", "", "Bot: @lo9i_bot"),
        ("text", "", "Nobody is paired yet."),
        ("code", "Pairing code", pairing.code),
        ("text", "", "Send this code to the bot in Telegram within 10 minutes."),
        ("action", "New pairing code", NEW_PAIRING_CODE),
    ]
    pairing.close()


async def test_a_new_code_from_the_app_and_a_pairing_update_the_status(tmp_path, bot):
    client, pairing = FakeClient(), await Pairing.load(tmp_path)
    channel = TelegramChannel(client, pairing, bot, "Bot: @lo9i_bot")
    first = pairing.code
    await channel.action(NEW_PAIRING_CODE)
    assert pairing.code != first and ("code", "Pairing code", pairing.code) in client.statuses[-1]
    await pairing.pair(pairing.code, 222, "Ani (@ani)")
    assert client.statuses[-1][1] == ("text", "", "Paired: Ani (@ani)")
    assert all(kind != "code" for kind, _, _ in client.statuses[-1])


async def test_without_a_bot_operations_say_why(tmp_path):
    pairing = await Pairing.load(tmp_path)
    channel = TelegramChannel(FakeClient(), pairing, None, "Telegram refused the bot token.")
    await channel.hello("Max")
    with pytest.raises(OperationError, match="refused"):
        await channel.operate("send", {"chat": "42", "text": "hi"})
    pairing.close()


async def _paired(tmp_path, ids):
    (tmp_path / FILE).write_text(json.dumps({"users": [{"id": i, "name": str(i)} for i in ids]}))
    return await Pairing.load(tmp_path)


async def test_the_users_own_chat_is_every_paired_account(tmp_path, bot):
    channel = TelegramChannel(FakeClient(), await _paired(tmp_path, [11, 22]), bot, "Bot: @lo9i_bot")
    buttons = [["✅ Approve", "ja:i1"], ["❌ Reject", "jr:i1"]]
    sent = await channel.operate("send", {"chat": "", "text": "The job is waiting", "buttons": buttons})
    assert sent == "7" and [c.args[0] for c in bot.send_message.call_args_list] == [11, 22]
    keys = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [k.callback_data for k in keys] == ["ja:i1", "jr:i1"]


async def test_nobody_paired_or_a_telegram_error_fails_the_operation(tmp_path, bot):
    channel = TelegramChannel(FakeClient(), await _paired(tmp_path, []), bot, "Bot: @lo9i_bot")
    with pytest.raises(OperationError, match="paired"):
        await channel.operate("send", {"chat": "", "text": "hi"})
    bot.send_message.side_effect = NetworkError("down")
    with pytest.raises(OperationError, match="down"):
        await channel.operate("send", {"chat": "42", "text": "hi"})


async def test_edits_typing_and_voice_go_to_the_chat(tmp_path, bot):
    channel = TelegramChannel(FakeClient(), await _paired(tmp_path, [11]), bot, "Bot: @lo9i_bot")
    await channel.operate("edit", {"chat": "42", "message": "7", "text": "Approve bash?", "buttons": []})
    edit = bot.edit_message_text.call_args
    assert (edit.kwargs["chat_id"], edit.kwargs["message_id"], edit.kwargs["reply_markup"]) == (42, 7, None)
    assert await channel.operate("typing", {"chat": "42"}) == ""
    await channel.operate("voice", {"chat": "42", "audio": "T2dnUw=="})
    assert bot.send_voice.call_args.args == (42, b"OggS")
