"""Who may use the bot: Telegram accounts paired with a one-time code, kept in
`$TELEGRAM_DATA/telegram.json`. While nobody is paired a code is always open, so the user can read it
in the app and send it to the bot; another account is paired with a new code from the app."""

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FILE = "telegram.json"
# Written by lo9i when it moves the accounts paired with its old built-in Telegram here.
IMPORT_FILE = "import.json"
LIFETIME = timedelta(minutes=10)

Listener = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class PairedUser:
    id: int
    name: str


class Pairing:
    def __init__(self, path: Path, users: list[PairedUser], lifetime: timedelta = LIFETIME) -> None:
        self._path = path
        self._users = users
        self._lifetime = lifetime
        self._code = ""
        self._expiry: asyncio.Task[None] | None = None
        self._listeners: list[Listener] = []

    @classmethod
    async def load(cls, folder: Path, lifetime: timedelta = LIFETIME) -> "Pairing":
        """The paired accounts, with any imported ones merged in. Opens a code if nobody is paired."""
        saved = await asyncio.to_thread(_read_users, folder / FILE)
        imported = await asyncio.to_thread(_take_import, folder / IMPORT_FILE)
        pairing = cls(folder / FILE, _merged(saved, imported), lifetime)
        if imported:
            await pairing._save()
        if not pairing.users:
            pairing._open()
        return pairing

    @property
    def users(self) -> list[PairedUser]:
        return list(self._users)

    @property
    def code(self) -> str:
        """The open pairing code, or "" when none is open."""
        return self._code

    @property
    def lifetime(self) -> timedelta:
        return self._lifetime

    def recipients(self) -> list[int]:
        """Paired accounts. Their private chat with the bot has the same id as the account."""
        return [u.id for u in self._users]

    def allowed(self, user_id: int) -> bool:
        return any(u.id == user_id for u in self._users)

    def subscribe(self, listener: Listener) -> None:
        """`listener` is called after every change: a pairing, a new code, a code expiring."""
        self._listeners.append(listener)

    async def open(self) -> str:
        """A new one-time code, to pair another account or replace an expired code."""
        self._open()
        await self._changed()
        return self._code

    async def pair(self, text: str, user_id: int, name: str) -> bool:
        """Pairs the sender if `text` contains the open pairing code. The code works once."""
        if not self._code or self._code.lower() not in text.lower():
            return False
        if not self.allowed(user_id):
            self._users.append(PairedUser(user_id, name))
            await self._save()
        self._close()
        await self._changed()
        return True

    def close(self) -> None:
        """Stops the code's timer, when the bot stops."""
        self._close()

    def _open(self) -> None:
        self._close()
        self._code = f"lo9i-{secrets.randbelow(9000) + 1000}"
        self._expiry = asyncio.create_task(self._expire())

    def _close(self) -> None:
        self._code = ""
        if self._expiry is not None and self._expiry is not asyncio.current_task():
            self._expiry.cancel()
        self._expiry = None

    async def _expire(self) -> None:
        """A code works for its lifetime; while nobody is paired a new one replaces it."""
        await asyncio.sleep(self._lifetime.total_seconds())
        if self._users:
            self._close()
        else:
            self._open()
        await self._changed()

    async def _changed(self) -> None:
        for listener in self._listeners:
            await listener()

    async def _save(self) -> None:
        await asyncio.to_thread(_write_users, self._path, self._users)


def _merged(saved: list[PairedUser], imported: list[PairedUser]) -> list[PairedUser]:
    known = {u.id for u in saved}
    return saved + [u for u in imported if u.id not in known]


def _read_users(path: Path) -> list[PairedUser]:
    if not path.exists():
        return []
    return _users(json.loads(path.read_text()))


def _take_import(path: Path) -> list[PairedUser]:
    """The imported accounts, removing the file so they're imported once."""
    if not path.exists():
        return []
    try:
        users = _users(json.loads(path.read_text()))
    except (ValueError, KeyError, TypeError) as e:
        logger.error("Couldn't import the paired Telegram accounts from %s: %s", path, e)
        return []
    path.unlink()
    logger.info("Imported %s paired Telegram accounts.", len(users))
    return users


def _users(data: dict[str, Any]) -> list[PairedUser]:
    return [PairedUser(int(u["id"]), str(u["name"])) for u in data.get("users", [])]


def _write_users(path: Path, users: list[PairedUser]) -> None:
    """Written whole to a temporary file first, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"users": [asdict(u) for u in users]}, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)
