"""Messages the assistant starts, and scheduled jobs' requests, go to every paired account."""

import pytest
from telegram.error import NetworkError

from lo9i_chat.runner import DeliveryError
from lo9i_telegram.approvals import TelegramApprovals
from lo9i_telegram.sender import TelegramSender
from tests.conftest import Paired


async def test_sends_to_every_paired_account(bot):
    await TelegramSender(bot, Paired([11, 22])).send("Stretch!")
    assert [c.args[0] for c in bot.send_message.call_args_list] == [11, 22]


async def test_no_paired_account_or_a_telegram_error_is_a_delivery_error(bot):
    with pytest.raises(DeliveryError, match="paired"):
        await TelegramSender(bot, Paired([])).send("hi")
    bot.send_message.side_effect = NetworkError("down")
    with pytest.raises(DeliveryError, match="down"):
        await TelegramSender(bot, Paired([11])).send("hi")


async def test_job_requests_go_to_every_paired_account_with_job_buttons(bot):
    request = {"tool": "bash", "command": "rm -rf build", "reason": "recursive delete"}
    await TelegramApprovals(bot, Paired([11])).forward("i1", "The job cleanup", request)
    call = bot.send_message.call_args
    assert call.args[0] == 11 and call.args[1].startswith("The job cleanup is waiting for you.")
    buttons = [b.callback_data for b in call.kwargs["reply_markup"].inline_keyboard[0]]
    assert buttons == ["ja:i1", "jr:i1"]


async def test_a_job_request_with_nobody_paired_is_a_delivery_error(bot):
    with pytest.raises(DeliveryError):
        await TelegramApprovals(bot, Paired([])).forward("i1", "The job x", {})
