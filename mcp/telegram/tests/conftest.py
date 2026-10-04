from unittest.mock import MagicMock, create_autospec

import pytest
from telegram import Bot, Message


@pytest.fixture
def bot() -> MagicMock:
    """An autospec of telegram.Bot, so every call's arguments are checked against the real API."""
    bot = create_autospec(Bot, instance=True)
    bot.send_message.return_value = create_autospec(Message, instance=True, message_id=7)
    return bot


class Paired:
    def __init__(self, recipients: list[int]) -> None:
        self._recipients = recipients

    def recipients(self) -> list[int]:
        return self._recipients
