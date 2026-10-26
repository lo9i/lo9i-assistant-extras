"""Whether the account is linked, and what linking it needs: the QR code to scan, or a code to type. The apps show
it in the channel's setup (status.py) and the page shows the QR code; the page runs in a process of its own, so
every change is also written to `$WHATSAPP_DATA/state.json`, where it reads it (web/app.py). The page asks for
a new QR code by creating `$WHATSAPP_DATA/new-qr`."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FILE = "state.json"
NEW_QR_FILE = "new-qr"

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
    # The channel isn't running.
    STOPPED = "stopped"


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
    def __init__(self, folder: Path) -> None:
        self._path = folder / FILE
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

    def stopped(self) -> None:
        """When the channel ends; nobody listens any more."""
        self._now = Snapshot(State.STOPPED)
        self._write()

    def asked_for_new_qr(self) -> bool:
        """Whether the page asked for a new QR code since the last time; the request is used up."""
        request = self._path.with_name(NEW_QR_FILE)
        try:
            request.unlink()
        except FileNotFoundError:
            return False
        return True

    async def _set(self, snapshot: Snapshot) -> None:
        if snapshot == self._now:
            return
        self._now = snapshot
        await asyncio.to_thread(self._write)
        for listener in self._listeners:
            await listener()

    def _write(self) -> None:
        """Written whole to a temporary file first, so the page never reads half of it. Only this user may read
        it: the QR code links the account."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(self._now)))
            temporary.chmod(0o600)
            temporary.replace(self._path)
        except OSError as e:
            logger.warning("Couldn't write %s: %s", self._path, e)


def read(folder: Path) -> Snapshot:
    """What the channel last wrote; STOPPED when it never ran."""
    try:
        data: dict[str, Any] = json.loads((folder / FILE).read_text())
        return Snapshot(**{**data, "state": State(data["state"])})
    except (OSError, ValueError, KeyError, TypeError):
        return Snapshot(State.STOPPED)


def ask_for_new_qr(folder: Path) -> None:
    """The page's request for a new QR code, which the channel picks up."""
    (folder / NEW_QR_FILE).touch()
