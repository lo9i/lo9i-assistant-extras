"""What the plugin keeps about hosts, in SQLite at HOSTS_DB (lo9i sets it to its data folder,
~/.lo9i/data/hosts/hosts.db): which ones the user hid, and when they last opened each one's terminal.
The hosts themselves are the SSH config's, read each time.
"""

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    name TEXT PRIMARY KEY,
    hidden INTEGER NOT NULL DEFAULT 0,
    last_connected TEXT
)
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or os.environ.get("HOSTS_DB") or str(Path.home() / ".lo9i" / "data" / "hosts" / "hosts.db")
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    return conn


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
