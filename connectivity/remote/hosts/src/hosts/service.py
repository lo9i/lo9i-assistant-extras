"""The hosts as the page and the tools see them: the SSH config's, each with where it goes, whether the
user hid it, and when its terminal was last opened. Only hosts in the config are reached, by their names
there, so nothing typed elsewhere becomes an ssh argument.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from . import db, sshconfig
from .sshconfig import Address


class UnknownHostError(Exception):
    pass


@dataclass(frozen=True)
class Host:
    name: str
    address: Address | None
    hidden: bool
    last_connected: str | None


async def all_hosts() -> list[Host]:
    """Every host in the SSH config, hidden ones too."""
    names = await asyncio.to_thread(sshconfig.names)
    addresses = await asyncio.gather(*(sshconfig.resolve(n) for n in names))
    hidden, connected = await asyncio.to_thread(_kept)
    return [Host(n, a, n in hidden, connected.get(n)) for n, a in zip(names, addresses, strict=True)]


async def shown_hosts() -> list[Host]:
    return [h for h in await all_hosts() if not h.hidden]


async def host(name: str, *, shown_only: bool = False) -> Host:
    """Raises UnknownHostError, naming the ones there are, for a name the config doesn't have."""
    hosts = await (shown_hosts() if shown_only else all_hosts())
    for h in hosts:
        if h.name == name:
            return h
    known = ", ".join(h.name for h in hosts) or "none"
    raise UnknownHostError(f"No host named {name!r}. The hosts are: {known}.")


async def set_hidden(name: str, hide: bool) -> None:
    await host(name)
    await asyncio.to_thread(_write, db.set_hidden, name, hide)


async def connected(name: str) -> None:
    await asyncio.to_thread(_write, db.connected, name)


def _kept() -> tuple[set[str], dict[str, str]]:
    conn = db.connect()
    try:
        return db.hidden(conn), db.last_connected(conn)
    finally:
        conn.close()


def _write(change: Callable[..., None], *args: object) -> None:
    conn = db.connect()
    try:
        change(conn, *args)
    finally:
        conn.close()
