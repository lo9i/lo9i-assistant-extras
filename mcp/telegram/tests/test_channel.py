"""The channel publishes what the apps show, opens pairing codes from the app and refuses deliveries
when there's no bot."""

import pytest

from lo9i_chat.runner import DeliveryError
from lo9i_telegram.channel import TelegramChannel
from lo9i_telegram.pairing import Pairing
from lo9i_telegram.status import NEW_PAIRING_CODE


class FakeClient:
    def __init__(self):
        self.statuses = []

    async def set_status(self, items):
        self.statuses.append([(i.kind, i.label, i.value) for i in items])


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


async def test_without_a_bot_deliveries_say_why(tmp_path):
    pairing = await Pairing.load(tmp_path)
    channel = TelegramChannel(FakeClient(), pairing, None, "Telegram refused the bot token.")
    await channel.hello("Max")
    with pytest.raises(DeliveryError, match="refused"):
        await channel.deliver("hi")
    pairing.close()
