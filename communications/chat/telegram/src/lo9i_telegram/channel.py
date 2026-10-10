"""What the Telegram bot does with lo9i's stream (runner.py): follows the assistant's name, shows its
commands in Telegram's menu, carries out chat operations, keeps the apps' status current and opens
pairing codes."""

import base64
import logging
from collections import OrderedDict
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
# Messages sent to the user's own chat whose copies in each paired account are remembered, for edits.
_COPIES_KEPT = 500


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
        # The id lo9i got for a message sent to its own chat → the message's id in each paired chat.
        self._copies: OrderedDict[str, dict[int, int]] = OrderedDict()
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
        bot = self._running_bot()
        if data["chat"] == _OWN_CHAT:
            return await self._operate_own(bot, kind, data)
        chat = TelegramChat(bot, int(data["chat"]))
        if kind == "send":
            return str(await _send(chat, data))
        if kind == "edit":
            await _edit(chat, int(data["message"]), data)
        else:
            await _other(chat, kind, data)
        return ""

    async def _operate_own(self, bot: Bot, kind: str, data: dict[str, Any]) -> str:
        """In every paired account. Each chat numbers its messages on its own, so a message sent to all
        of them has a different id in each: lo9i gets the last chat's, and edits reach each chat's copy."""
        recipients = self._recipients()
        if kind == "send":
            copies = {chat_id: await _send(TelegramChat(bot, chat_id), data) for chat_id in recipients}
            sent = str(copies[recipients[-1]])
            self._copies[sent] = copies
            while len(self._copies) > _COPIES_KEPT:
                self._copies.popitem(last=False)
            return sent
        if kind == "edit":
            # A message sent before the bot restarted: only the last chat's id is known.
            copies = self._copies.get(data["message"]) or {recipients[-1]: int(data["message"])}
            for chat_id, message_id in copies.items():
                await _edit(TelegramChat(bot, chat_id), message_id, data)
        else:
            for chat_id in recipients:
                await _other(TelegramChat(bot, chat_id), kind, data)
        return ""

    def _recipients(self) -> list[int]:
        if not (recipients := self._pairing.recipients()):
            raise OperationError(_NOBODY_PAIRED)
        return recipients

    def _running_bot(self) -> Bot:
        if self._bot is None:
            raise OperationError(self._bot_line)
        return self._bot


async def _send(chat: TelegramChat, data: dict[str, Any]) -> int:
    return await chat.send(data["text"], data.get("markdown", False), data.get("buttons"))


async def _edit(chat: TelegramChat, message_id: int, data: dict[str, Any]) -> None:
    await chat.edit(message_id, data["text"], data.get("markdown", False), data.get("buttons"))


async def _other(chat: TelegramChat, kind: str, data: dict[str, Any]) -> None:
    if kind == "typing":
        await chat.typing()
    elif kind == "voice":
        await chat.voice(base64.b64decode(data["audio"]))
