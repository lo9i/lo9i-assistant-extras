"""The hosts as the page and the tools see them: the plugin's own (db.py), each with where it goes, whether the
user hid it, and when its terminal was last opened; and their tunnels. The user's SSH config is imported once,
the first time they're asked for, and again only when the user asks (`reimport`), which adds the hosts the list
doesn't have and changes none. Only hosts in the list are reached, by their names there, so nothing typed
elsewhere becomes an ssh argument.
"""

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from . import db, sshconfig, vault
from .details import Address, Details, config_block
from .errors import UnknownHostError
from .tunnels import Tunnel

__all__ = ["UnknownHostError"]


class UnknownTunnelError(Exception):
    pass


@dataclass(frozen=True)
class Host:
    name: str
    address: Address | None
    hidden: bool
    last_connected: str | None
    # How it signs in, for the page: its key file's path ("" for ssh's own), and whether it has a password.
    key: str = ""
    password: bool = False


async def all_hosts() -> list[Host]:
    """Every host, hidden ones too, by name."""
    return await asyncio.to_thread(_db, _all)


async def shown_hosts() -> list[Host]:
    return [h for h in await all_hosts() if not h.hidden]


async def host(name: str, *, shown_only: bool = False) -> Host:
    """Raises UnknownHostError, naming the ones there are, for a name the list doesn't have."""
    hosts = await (shown_hosts() if shown_only else all_hosts())
    for h in hosts:
        if h.name == name:
            return h
    known = ", ".join(h.name for h in hosts) or "none"
    raise UnknownHostError(f"No host named {name!r}. The hosts are: {known}.")


async def details(name: str) -> Details:
    """The host's details, its password too. Raises UnknownHostError."""
    return await asyncio.to_thread(_db, _details, name)


async def add_host(name: str, found: Details) -> None:
    """Raises db.DuplicateHostError for a name the list has already."""
    await asyncio.to_thread(_db, _add, name, found)


async def change_host(name: str, new_name: str, found: Details) -> None:
    """Raises UnknownHostError, or db.DuplicateHostError for a new name another host has."""
    await host(name)
    await asyncio.to_thread(_db, db.change_host, name, new_name, vault.encrypt(found.to_json()))


async def delete_host(name: str) -> None:
    """The host and its tunnels."""
    await host(name)
    await asyncio.to_thread(_db, db.delete_host, name)


async def reimport() -> list[str]:
    """Adds the SSH config's hosts the list doesn't have, by name; the ones it has stay as they are. Gives
    the names added."""
    return await asyncio.to_thread(_db, _import)


async def ssh_config(name: str) -> tuple[str, str]:
    """An ssh config with every host, for connecting to this one (connect.py): every host, so one it names to
    jump through (ProxyJump) is there too; and this host's password, "" for none. Raises UnknownHostError."""
    return await asyncio.to_thread(_db, _ssh_config, name)


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


def _all(conn: sqlite3.Connection) -> list[Host]:
    _first_import(conn)
    hosts = []
    for name, token, hidden, connected in db.hosts(conn):
        found = Details.from_json(vault.decrypt(token))
        hosts.append(Host(name, found.address, hidden, connected, found.identity_file, bool(found.password)))
    return hosts


def _details(conn: sqlite3.Connection, name: str) -> Details:
    _first_import(conn)
    token = db.host(conn, name)
    if token is None:
        raise UnknownHostError(f"No host named {name!r}.")
    return Details.from_json(vault.decrypt(token))


def _add(conn: sqlite3.Connection, name: str, found: Details) -> None:
    _first_import(conn)
    if not db.add_host(conn, name, vault.encrypt(found.to_json())):
        raise db.DuplicateHostError(f"There's a host named {name} already.")


def _ssh_config(conn: sqlite3.Connection, name: str) -> tuple[str, str]:
    _first_import(conn)
    every = {n: Details.from_json(vault.decrypt(token)) for n, token, _, _ in db.hosts(conn)}
    if name not in every:
        raise UnknownHostError(f"No host named {name!r}.")
    text = "# Written by the hosts plugin for one connection, from its own list; deleted when it ends.\n\n"
    return text + "\n".join(config_block(n, d) for n, d in every.items()), every[name].password


def _first_import(conn: sqlite3.Connection) -> None:
    """The SSH config's hosts, the first time the list is opened, whatever it has then."""
    if not db.imported(conn):
        _import(conn)


def _import(conn: sqlite3.Connection) -> list[str]:
    added = [name for name, found in sshconfig.hosts() if db.add_host(conn, name, vault.encrypt(found.to_json()))]
    db.mark_imported(conn)
    return added


def _db[T](use: Callable[..., T], *args: object) -> T:
    """Calls `use` with a connection to the database, and the args."""
    conn = db.connect()
    try:
        return use(conn, *args)
    finally:
        conn.close()
