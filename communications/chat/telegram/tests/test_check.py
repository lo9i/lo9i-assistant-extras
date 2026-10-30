"""The check lo9i runs before the bot token is saved: a fake Telegram, so nothing reaches the network."""

import json

from telegram.error import NetworkError
from telegram.request import BaseRequest

from lo9i_telegram import check
from lo9i_telegram.app import REFUSED

GOOD = "123456:good"
_BOT = {"id": 123456, "is_bot": True, "first_name": "Assistant", "username": "my_assistant_bot"}


class Telegram(BaseRequest):
    """Knows the bot whose token is GOOD; can't be reached with `offline`."""

    def __init__(self, offline: bool = False) -> None:
        self.offline = offline
        self.asked: list[str] = []

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 5

    async def do_request(self, url, method, request_data=None, **timeouts) -> tuple[int, bytes]:
        self.asked.append(url.rsplit("/", 1)[-1])
        if self.offline:
            raise NetworkError("no network")
        if f"/bot{GOOD}/" not in url:
            return 401, json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized"}).encode()
        return 200, json.dumps({"ok": True, "result": _BOT}).encode()


async def test_a_token_telegram_knows_passes():
    telegram = Telegram()
    assert await check.problem(f" {GOOD} ", telegram) is None
    assert telegram.asked == ["getMe"]


async def test_a_token_telegram_refuses_says_so():
    assert await check.problem("123456:wrong", Telegram()) == REFUSED
    assert await check.problem("", Telegram()) == REFUSED


async def test_when_telegram_cant_be_reached_the_token_goes_through():
    assert await check.problem(GOOD, Telegram(offline=True)) is None
