"""Writing to someone other than the user, as the user, when the assistant asks and the user approves each
message (the plugin's tools, mcp_server.py, which reach this through the channel's process, tools.py). Their
chat is never read: the assistant doesn't see the answer, which reaches the user's phone as any message does.

Only people (no groups), only numbers on WhatsApp, at most OTHERS_PER_HOUR an hour, and written as slowly as
everything the account sends: an account that writes to many people in a short time is what WhatsApp bans."""

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from lo9i_whatsapp.incoming import LID_SERVER, PHONE_SERVER, Me
from lo9i_whatsapp.tools import ToolError

OTHERS_PER_HOUR = 20
_HOUR = 3600.0
# Contacts a search lists at most.
_FOUND_MOST = 15


@dataclass(frozen=True)
class Found:
    name: str
    # As +5491112345678; "" when WhatsApp keeps only the contact's private id.
    phone: str


class Account(Protocol):
    """What this needs from the WhatsApp connection (whatsapp.py)."""

    def account(self) -> tuple[Any, Me]: ...
    async def write(self, jid: Any, text: str) -> str: ...


class Others:
    def __init__(self, whatsapp: Account, clock: Callable[[], float] = time.monotonic) -> None:
        self._whatsapp = whatsapp
        self._clock = clock
        self._sent: deque[float] = deque()

    async def send(self, to: str, text: str) -> str:
        """`to` is a phone number with its country code. Returns where it went, as +number."""
        number = re.sub(r"\D", "", to)
        client, me = self._account()
        if len(number) < 8:
            raise ToolError(f"{to!r} isn't a phone number with its country code, like +5491112345678.")
        if number == me.phone:
            raise ToolError("That's the user's own number: write to them in this chat instead.")
        if not text.strip():
            raise ToolError("The message is empty.")
        jid = await _on_whatsapp(client, number)
        self._take_turn()
        await self._whatsapp.write(jid, text)
        return f"+{number}"

    async def find(self, query: str) -> list[Found]:
        """The account's contacts whose name has every word of `query`."""
        client, _ = self._account()
        words = query.casefold().split()
        found: list[Found] = []
        for contact in await client.contact.get_all_contacts():
            name = _name(contact.Info)
            if name and all(word in name.casefold() for word in words):
                found.append(Found(name, await _phone(client, contact.JID)))
        return sorted(found, key=lambda f: f.name.casefold())[:_FOUND_MOST]

    def _take_turn(self) -> None:
        now = self._clock()
        while self._sent and now - self._sent[0] > _HOUR:
            self._sent.popleft()
        if len(self._sent) >= OTHERS_PER_HOUR:
            raise ToolError(
                f"Already {OTHERS_PER_HOUR} messages to other people in the last hour; WhatsApp bans accounts that "
                "write to many people in a short time. Try again later."
            )
        self._sent.append(now)

    def _account(self) -> tuple[Any, Me]:
        try:
            return self._whatsapp.account()
        except ConnectionError as e:
            raise ToolError("WhatsApp isn't connected; the user links it in the WhatsApp plugin's setup.") from e


async def _on_whatsapp(client: Any, number: str) -> Any:
    answers = await client.is_on_whatsapp(number)
    if not answers or not answers[0].IsIn:
        raise ToolError(f"+{number} isn't on WhatsApp.")
    return answers[0].JID


def _name(info: Any) -> str:
    return info.FullName or info.FirstName or info.PushName or info.BusinessName


async def _phone(client: Any, jid: Any) -> str:
    if jid.Server == PHONE_SERVER:
        return f"+{jid.User}"
    if jid.Server == LID_SERVER:
        try:
            phone = await client.get_pn_from_lid(jid)
        except Exception:
            return ""
        return f"+{phone.User}" if phone.User else ""
    return ""
