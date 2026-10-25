"""What the plugin keeps, in SQLite at HOSTS_DB (lo9i sets it to its data folder,
~/.lo9i/data/hosts/hosts.db): the hosts, each one's name readable and its details (details.py) encrypted with
the plugin's key (vault.py), whether the user hid it and when they last opened its terminal; and their tunnels
(tunnels.py). The hosts were imported once from the user's SSH config (service.py); since then they're only
these.

A database from before the plugin kept its hosts has rows for hosts only hidden or opened, with no details:
they aren't hosts until the import gives them some, keeping what the user set.
"""

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .tunnels import Tunnel

# Each one brings a database from the version before (its PRAGMA user_version) to the next. The first
# is the hosts table of the databases before there were versions, which already have it.
_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS hosts (
        name TEXT PRIMARY KEY,
        hidden INTEGER NOT NULL DEFAULT 0,
        last_connected TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tunnels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('local', 'remote', 'socks')),
        listen_port INTEGER NOT NULL,
        target_host TEXT NOT NULL DEFAULT '',
        target_port INTEGER,
        UNIQUE (host, name)
    )
    """,
    # Each host's details, encrypted; NULL for a row from before the hosts were kept here.
    "ALTER TABLE hosts ADD COLUMN details TEXT",
    # Whether the SSH config was imported (`imported`), so it's done once, even if every host is deleted after.
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
_TUNNEL = "SELECT id, host, name, kind, listen_port, target_host, target_port FROM tunnels"


class DuplicateTunnelError(Exception):
    pass


class DuplicateHostError(Exception):
    pass


def default_path() -> Path:
    return Path(os.environ.get("HOSTS_DB") or Path.home() / ".lo9i" / "data" / "hosts" / "hosts.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or str(default_path())
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Brings the database to the last version, holding its write lock: the page and the tools, and the page's
    requests, may open it at once, and a step like adding a column can't run twice."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= len(_MIGRATIONS):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, step in enumerate(_MIGRATIONS[version:], version + 1):
            conn.execute(step)
            conn.execute(f"PRAGMA user_version = {number}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def hosts(conn: sqlite3.Connection) -> list[tuple[str, str, bool, str | None]]:
    """Each host's name, encrypted details, whether it's hidden and when it was last connected, by name."""
    rows = conn.execute(
        "SELECT name, details, hidden, last_connected FROM hosts WHERE details IS NOT NULL ORDER BY name COLLATE NOCASE"
    )
    return [(name, details, bool(hidden), connected) for name, details, hidden, connected in rows]


def host(conn: sqlite3.Connection, name: str) -> str | None:
    """The host's encrypted details; None when there's no such host."""
    row = conn.execute("SELECT details FROM hosts WHERE name = ? AND details IS NOT NULL", (name,)).fetchone()
    return row[0] if row else None


def add_host(conn: sqlite3.Connection, name: str, details: str) -> bool:
    """Adds it, or gives its details to the row it has from before hosts were kept here; False, changing
    nothing, when there's a host by that name already."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO hosts (name, details) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET details = excluded.details WHERE hosts.details IS NULL",
            (name, details),
        )
    return cursor.rowcount > 0


def change_host(conn: sqlite3.Connection, name: str, new_name: str, details: str) -> None:
    """Its details, and its name, which its tunnels follow. Raises DuplicateHostError for a name another host
    has."""
    with conn:
        if new_name != name:
            if host(conn, new_name) is not None:
                raise DuplicateHostError(f"There's a host named {new_name} already.")
            conn.execute("DELETE FROM hosts WHERE name = ?", (new_name,))
            conn.execute("UPDATE hosts SET name = ? WHERE name = ?", (new_name, name))
            conn.execute("UPDATE tunnels SET host = ? WHERE host = ?", (new_name, name))
        conn.execute("UPDATE hosts SET details = ? WHERE name = ?", (details, new_name))


def delete_host(conn: sqlite3.Connection, name: str) -> None:
    """The host and its tunnels."""
    with conn:
        conn.execute("DELETE FROM tunnels WHERE host = ?", (name,))
        conn.execute("DELETE FROM hosts WHERE name = ?", (name,))


def imported(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM meta WHERE key = 'imported'").fetchone() is not None


def mark_imported(conn: sqlite3.Connection, when: datetime | None = None) -> None:
    moment = (when or datetime.now(UTC)).isoformat(timespec="seconds")
    with conn:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('imported', ?)", (moment,))


def hidden(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM hosts WHERE hidden")}


def last_connected(conn: sqlite3.Connection) -> dict[str, str]:
    """ISO times in UTC, by host."""
    return dict(conn.execute("SELECT name, last_connected FROM hosts WHERE last_connected IS NOT NULL").fetchall())


def set_hidden(conn: sqlite3.Connection, name: str, hide: bool) -> None:
    with conn:
        conn.execute(
            "INSERT INTO hosts (name, hidden) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET hidden = excluded.hidden",
            (name, int(hide)),
        )


def connected(conn: sqlite3.Connection, name: str, when: datetime | None = None) -> None:
    moment = (when or datetime.now(UTC)).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO hosts (name, last_connected) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_connected = excluded.last_connected",
            (name, moment),
        )


def tunnels(conn: sqlite3.Connection, host: str | None = None) -> list[Tunnel]:
    """A host's tunnels, or every host's, by host and name."""
    if host is None:
        rows = conn.execute(f"{_TUNNEL} ORDER BY host, name COLLATE NOCASE")
    else:
        rows = conn.execute(f"{_TUNNEL} WHERE host = ? ORDER BY name COLLATE NOCASE", (host,))
    return [Tunnel(*row) for row in rows]


def tunnel(conn: sqlite3.Connection, tunnel_id: int) -> Tunnel | None:
    row = conn.execute(f"{_TUNNEL} WHERE id = ?", (tunnel_id,)).fetchone()
    return Tunnel(*row) if row else None


def save_tunnel(conn: sqlite3.Connection, t: Tunnel) -> Tunnel:
    """Adds it (no id) or changes the one with its id; gives it back with its id. A host's tunnels have
    names of their own: raises DuplicateTunnelError for one it has already."""
    values = (t.name, t.kind, t.listen_port, t.target_host, t.target_port)
    try:
        with conn:
            if t.id is None:
                cursor = conn.execute(
                    "INSERT INTO tunnels (host, name, kind, listen_port, target_host, target_port) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (t.host, *values),
                )
                tunnel_id = cursor.lastrowid
            else:
                conn.execute(
                    "UPDATE tunnels SET name = ?, kind = ?, listen_port = ?, target_host = ?, target_port = ? "
                    "WHERE id = ?",
                    (*values, t.id),
                )
                tunnel_id = t.id
    except sqlite3.IntegrityError:
        raise DuplicateTunnelError(f"{t.host} has a tunnel named {t.name!r} already.") from None
    saved = tunnel(conn, tunnel_id)  # type: ignore[arg-type]
    assert saved is not None
    return saved


def delete_tunnel(conn: sqlite3.Connection, tunnel_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM tunnels WHERE id = ?", (tunnel_id,))
