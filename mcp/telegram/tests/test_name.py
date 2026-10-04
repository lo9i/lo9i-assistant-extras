"""The bot's Telegram display name follows the assistant's name."""

from telegram.error import NetworkError

from lo9i_telegram.name import BotName


async def test_sets_the_name_only_when_it_changes(bot):
    name = BotName()
    await name.apply(bot, "Max")
    await name.apply(bot, "Max")  # every reconnection says the name again
    await name.apply(bot, "Ada")
    assert [c.args for c in bot.set_my_name.call_args_list] == [("Max",), ("Ada",)]


async def test_a_telegram_error_is_retried_at_the_next_change(bot):
    bot.set_my_name.side_effect = NetworkError("down")
    name = BotName()
    await name.apply(bot, "Max")
    bot.set_my_name.side_effect = None
    await name.apply(bot, "Max")
    assert bot.set_my_name.call_count == 2
