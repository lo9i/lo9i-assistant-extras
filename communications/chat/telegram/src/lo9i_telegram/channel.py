"""What the Telegram bot does with lo9i's stream (runner.py): follows the assistant's name, shows its
commands in Telegram's menu, carries out chat operations, keeps the apps' status current and opens
pairing codes."""

import base64
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from telegram import Bot, BotCommand
from telegram.error import TelegramError

from lo9i_telegram.daemon import DaemonError
from lo9i_telegram.name import BotName
from lo9i_telegram.pairing import Pairing
from lo9i_telegram.port import TelegramChat
from lo9i_telegram.runner import Commands, OperationError
from lo9i_telegram.status import NEW_PAIRING_CODE, items

logger = logging.getLogger(__name__)

# lo9i's chat for the user: every paired account, for messages and requests the assistant starts.
_OWN_CHAT = ""
_NOBODY_PAIRED = "No Telegram account is paired yet. The user can pair one in the app's Telegram setup."


class StatusSink(Protocol):
    """The Daemon call the channel needs."""

    async def set_status(self, items: Sequence[dict[str, str]]) -> None: ...


class TelegramChannel:
    def __init__(self, daemon: StatusSink, pairing: Pairing, bot: Bot | None, bot_line: str) -> None:
        """`bot` is None when Telegram refused the token; `bot_line` then says so."""
        self._daemon = daemon
        self._pairing = pairing
        self._bot = bot
        self._bot_line = bot_line
        self._name = BotName()
        pairing.subscribe(self.publish_status)

    async def hello(self, assistant_name: str, commands: Commands) -> None:
        await self.renamed(assistant_name)
        await self.commands(commands)
        await self.publish_status()

    async def renamed(self, assistant_name: str) -> None:
        if self._bot is not None:
            await self._name.apply(self._bot, assistant_name)

    async def commands(self, commands: Commands) -> None:
        """Telegram's command menu: /new, /home and each agent's command. Any command typed goes to lo9i."""
        if self._bot is None:
            return
        try:
            await self._bot.set_my_commands([BotCommand(c["command"], c["description"]) for c in commands])
        except TelegramError as e:
            logger.warning("Couldn't set the bot's commands: %s", e)

    async def action(self, action: str) -> None:
        if action == NEW_PAIRING_CODE:
            await self._pairing.open()
        else:
            logger.warning("Unknown action %r", action)

    async def operate(self, kind: str, data: dict[str, Any]) -> str:
        try:
            return await self._operate(kind, data)
        except TelegramError as e:
            raise OperationError(f"Telegram didn't take it: {e}") from e

    async def publish_status(self) -> None:
        """While lo9i is away this fails quietly; every new stream starts with `hello`, which publishes it
        again."""
        try:
            await self._daemon.set_status(items(self._bot_line, self._pairing))
        except DaemonError as e:
            logger.warning("Couldn't send the status: %s", e)

    async def _operate(self, kind: str, data: dict[str, Any]) -> str:
        chats = [TelegramChat(self._running_bot(), chat_id) for chat_id in self._chat_ids(data["chat"])]
        if kind == "send":
            sent = [await chat.send(data["text"], data.get("markdown", False), data.get("buttons")) for chat in chats]
            return str(sent[-1])
        for chat in chats:
            if kind == "edit":
                await chat.edit(int(data["message"]), data["text"], data.get("markdown", False), data.get("buttons"))
            elif kind == "typing":
                await chat.typing()
            elif kind == "voice":
                await chat.voice(base64.b64decode(data["audio"]))
        return ""

    def _chat_ids(self, chat: str) -> list[int]:
        if chat != _OWN_CHAT:
            return [int(chat)]
        if not (recipients := self._pairing.recipients()):
            raise OperationError(_NOBODY_PAIRED)
        return recipients

    def _running_bot(self) -> Bot:
        if self._bot is None:
            raise OperationError(self._bot_line)
        return self._bot
