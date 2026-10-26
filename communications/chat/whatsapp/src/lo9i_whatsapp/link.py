"""Whether the account is linked, and what linking it needs: the QR code to scan, or a code to type. The apps show
it in the channel's setup (status.py), the QR code included, so every change is published."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum

Listener = Callable[[], Awaitable[None]]


class State(StrEnum):
    # Connecting to WhatsApp, linked or not.
    STARTING = "starting"
    # Not linked: a QR code (and a code to type, when asked for) is waiting to be scanned.
    WAITING = "waiting"
    # Not linked, and the QR codes ran out; a new one comes when the user asks.
    EXPIRED = "expired"
    LINKED = "linked"
    # WhatsApp won't connect the account (a ban, an outdated version); `problem` says why.
    FAILED = "failed"


@dataclass(frozen=True)
class Snapshot:
    state: State = State.STARTING
    qr: str = ""
    # A code to type in WhatsApp instead of scanning the QR code.
    code: str = ""
    # The linked number, as +5491112345678, and the account's name.
    phone: str = ""
    name: str = ""
    # Why the account isn't linked or doesn't connect, for the user.
    problem: str = ""


class Link:
    def __init__(self) -> None:
        self._now = Snapshot()
        self._listeners: list[Listener] = []

    @property
    def now(self) -> Snapshot:
        return self._now

    def subscribe(self, listener: Listener) -> None:
        """`listener` is called after every change."""
        self._listeners.append(listener)

    async def starting(self) -> None:
        await self._set(Snapshot(State.STARTING))

    async def waiting(self, qr: str) -> None:
        """A new QR code. A code to type stays until the QR codes run out or the account is linked."""
        code = self._now.code if self._now.state == State.WAITING else ""
        await self._set(Snapshot(State.WAITING, qr=qr, code=code, problem=self._now.problem))

    async def code(self, code: str) -> None:
        await self._set(replace(self._now, code=code))

    async def note_problem(self, problem: str) -> None:
        """Something went wrong that doesn't change the state, such as a link code WhatsApp didn't give."""
        await self._set(replace(self._now, problem=problem))

    async def expired(self) -> None:
        await self._set(Snapshot(State.EXPIRED, problem=self._now.problem))

    async def linked(self, phone: str, name: str) -> None:
        await self._set(Snapshot(State.LINKED, phone=phone, name=name))

    async def unlinked(self, problem: str) -> None:
        """The account was unlinked (from the phone, or with Unlink); a new QR code follows."""
        await self._set(Snapshot(State.STARTING, problem=problem))

    async def failed(self, problem: str) -> None:
        await self._set(Snapshot(State.FAILED, phone=self._now.phone, name=self._now.name, problem=problem))

    async def _set(self, snapshot: Snapshot) -> None:
        if snapshot == self._now:
            return
        self._now = snapshot
        for listener in self._listeners:
            await listener()
