"""What the WhatsApp channel does with lo9i's stream (runner.py): keeps the apps' status current, carries out the
setup buttons (a new QR code, a code to type, unlinking) and the chat operations. Every chat lo9i names is the
user's chat with themself, the only one the channel writes in."""

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from lo9i_whatsapp.daemon import DaemonError
from lo9i_whatsapp.link import Link, State
from lo9i_whatsapp.port import SelfChat
from lo9i_whatsapp.runner import Commands, OperationError
from lo9i_whatsapp.status import LINK_CODE, NEW_QR, UNLINK, items

logger = logging.getLogger(__name__)

# lo9i's chat for the user, for messages and requests the assistant starts.
_OWN_CHAT = ""
_NOT_LINKED = "WhatsApp isn't linked yet. The user can link it in the app's WhatsApp setup."


class StatusSink(Protocol):
    """The Daemon call the channel needs."""

    async def set_status(self, items: Sequence[dict[str, str]]) -> None: ...


class Controls(Protocol):
    """What the setup buttons do to the WhatsApp connection (whatsapp.py)."""

    @property
    def chat(self) -> str:
        """The user's own chat's id, as the channel gives it to lo9i; "" while not linked."""
        ...

    async def new_qr(self) -> None: ...
    async def link_code(self) -> None: ...
    async def unlink(self) -> None: ...


class WhatsAppChannel:
    def __init__(self, daemon: StatusSink, link: Link, controls: Controls, chat: SelfChat, phone_given: bool) -> None:
        """`phone_given`: the user entered their number, so the setup offers a code to type."""
        self._daemon = daemon
        self._link = link
        self._controls = controls
        self._chat = chat
        self._phone_given = phone_given
        link.subscribe(self.publish_status)

    async def language(self, language: str) -> None:
        """Its own text is in English whatever the language the user chose."""

    async def hello(self, assistant_name: str, commands: Commands) -> None:
        await self.publish_status()

    async def renamed(self, assistant_name: str) -> None:
        """Messages carry no name in the user's own chat."""

    async def commands(self, commands: Commands) -> None:
        """WhatsApp has no command menu; any command typed goes to lo9i."""

    async def action(self, action: str) -> None:
        if action == NEW_QR:
            await self._controls.new_qr()
        elif action == LINK_CODE:
            await self._controls.link_code()
        elif action == UNLINK:
            await self._controls.unlink()
        else:
            logger.warning("Unknown action %r", action)

    async def operate(self, kind: str, data: dict[str, Any]) -> str:
        self._check_chat(data["chat"])
        if kind == "send":
            return await self._chat.send(data["text"], data.get("markdown", False), data.get("buttons"))
        if kind == "edit":
            await self._chat.edit(data["message"], data["text"], data.get("markdown", False), data.get("buttons"))
        elif kind == "voice":
            raise OperationError("The WhatsApp channel doesn't send voice notes.")
        # Typing isn't shown: it would mean telling WhatsApp the account is online, which stops the phone's
        # notifications.
        return ""

    async def publish_status(self) -> None:
        """While lo9i is away this fails quietly; every new stream starts with `hello`, which publishes it
        again."""
        try:
            await self._daemon.set_status(items(self._link.now, self._phone_given))
        except DaemonError as e:
            logger.warning("Couldn't send the status: %s", e)

    def _check_chat(self, chat: str) -> None:
        """Only the user's own chat, whatever lo9i names: nothing is ever sent anywhere else."""
        if self._link.now.state != State.LINKED or not self._controls.chat:
            raise OperationError(self._link.now.problem or _NOT_LINKED)
        if chat not in (_OWN_CHAT, self._controls.chat):
            raise OperationError("The WhatsApp channel only writes in the user's chat with themself.")
