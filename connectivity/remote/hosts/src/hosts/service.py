"""The hosts as the page and the tools see them: the SSH config's, each with where it goes, whether the
user hid it, and when its terminal was last opened; and their tunnels. Only hosts in the config are
reached, by their names there, so nothing typed elsewhere becomes an ssh argument.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from . import db, sshconfig
from .sshconfig import Address
from .tunnels import Tunnel


class UnknownHostError(Exception):
    pass


class UnknownTunnelError(Exception):
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
    await asyncio.to_thread(_db, db.set_hidden, name, hide)


async def connected(name: str) -> None:
    await asyncio.to_thread(_db, db.connected, name)


async def tunnels(host: str | None = None) -> list[Tunnel]:
    """A host's tunnels, or every host's."""
    return await asyncio.to_thread(_db, db.tunnels, host)


async def tunnel(host: str, tunnel_id: int) -> Tunnel:
    """The host's tunnel with this id; raises UnknownTunnelError if it has none."""
    found = await asyncio.to_thread(_db, db.tunnel, tunnel_id)
    if found is None or found.host != host:
        raise UnknownTunnelError(f"{host} has no tunnel {tunnel_id}.")
    return found


async def save_tunnel(t: Tunnel) -> Tunnel:
    """Raises db.DuplicateTunnelError for a name the host's tunnels have already."""
    await host(t.host)
    return await asyncio.to_thread(_db, db.save_tunnel, t)


async def delete_tunnel(tunnel_id: int) -> None:
    await asyncio.to_thread(_db, db.delete_tunnel, tunnel_id)


def _kept() -> tuple[set[str], dict[str, str]]:
    conn = db.connect()
    try:
        return db.hidden(conn), db.last_connected(conn)
    finally:
        conn.close()


def _db[T](use: Callable[..., T], *args: object) -> T:
    """Calls `use` with a connection to the database, and the args."""
    conn = db.connect()
    try:
        return use(conn, *args)
    finally:
        conn.close()
