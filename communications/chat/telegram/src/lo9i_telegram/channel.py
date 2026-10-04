"""What the Telegram bot does with the daemon's stream (lo9i_chat.runner.Channel): follows the
assistant's name, delivers messages and job requests to the paired accounts, keeps the apps' status
current and opens pairing codes."""

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from telegram import Bot

from lo9i_chat.errors import DaemonError
from lo9i_chat.runner import DeliveryError
from lo9i_chat.status import SetupItem
from lo9i_telegram.approvals import TelegramApprovals
from lo9i_telegram.name import BotName
from lo9i_telegram.pairing import Pairing
from lo9i_telegram.sender import TelegramSender
from lo9i_telegram.status import NEW_PAIRING_CODE, items

logger = logging.getLogger(__name__)


class StatusSink(Protocol):
    """The ChannelClient call the channel needs."""

    async def set_status(self, items: Sequence[SetupItem]) -> None: ...


class TelegramChannel:
    def __init__(self, client: StatusSink, pairing: Pairing, bot: Bot | None, bot_line: str) -> None:
        """`bot` is None when Telegram refused the token; `bot_line` then says so."""
        self._client = client
        self._pairing = pairing
        self._bot = bot
        self._bot_line = bot_line
        self._name = BotName()
        pairing.subscribe(self.publish_status)

    async def hello(self, assistant_name: str) -> None:
        await self.renamed(assistant_name)
        await self.publish_status()

    async def renamed(self, assistant_name: str) -> None:
        if self._bot is not None:
            await self._name.apply(self._bot, assistant_name)

    async def deliver(self, text: str) -> None:
        await TelegramSender(self._running_bot(), self._pairing).send(text)

    async def approval(self, interrupt_id: str, title: str, request: dict[str, Any]) -> None:
        await TelegramApprovals(self._running_bot(), self._pairing).forward(interrupt_id, title, request)

    async def action(self, action: str) -> None:
        if action == NEW_PAIRING_CODE:
            await self._pairing.open()
        else:
            logger.warning("Unknown action %r", action)

    async def publish_status(self) -> None:
        """While the daemon is away this fails quietly; every new stream starts with `hello`, which
        publishes it again."""
        try:
            await self._client.set_status(items(self._bot_line, self._pairing))
        except DaemonError as e:
            logger.warning("Couldn't send the status: %s", e)

    def _running_bot(self) -> Bot:
        if self._bot is None:
            raise DeliveryError(self._bot_line)
        return self._bot
