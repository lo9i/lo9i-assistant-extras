"""SQL only. Validation and rules live in `service`."""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from .models import Channel, Title, Video


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- Titles ---


def list_titles(conn: sqlite3.Connection, kind: str | None = None, status: str | None = None) -> list[Title]:
    rows = conn.execute(
        "SELECT * FROM titles WHERE (?1 IS NULL OR kind = ?1) AND (?2 IS NULL OR status = ?2)"
        " ORDER BY name COLLATE NOCASE",
        (kind, status),
    )
    return [Title.from_row(r) for r in rows]


def get_title(conn: sqlite3.Connection, id: int) -> Title | None:
    row = conn.execute("SELECT * FROM titles WHERE id = ?", (id,)).fetchone()
    return Title.from_row(row) if row else None


def find_title(conn: sqlite3.Connection, kind: str, tmdb_id: int) -> Title | None:
    row = conn.execute("SELECT * FROM titles WHERE kind = ? AND tmdb_id = ?", (kind, tmdb_id)).fetchone()
    return Title.from_row(row) if row else None


def insert_title(
    conn: sqlite3.Connection, *, kind: str, tmdb_id: int, name: str, status: str, notes: str, info: dict
) -> int:
    ts = now()
    cur = conn.execute(
        "INSERT INTO titles (kind, tmdb_id, name, status, notes, info, refreshed_at, added_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, tmdb_id, name, status, notes, json.dumps(info), ts, ts, ts),
    )
    return cur.lastrowid


def update_title(conn: sqlite3.Connection, id: int, fields: dict[str, Any]) -> None:
    """UPDATE the given columns. Column names come from code, never input."""
    if not fields:
        return
    fields = {**fields, "updated_at": now()}
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE titles SET {sets} WHERE id = ?", [*fields.values(), id])


def save_info(conn: sqlite3.Connection, id: int, name: str, info: dict) -> None:
    """What TMDB says now. Not a change of the user's, so updated_at stays."""
    conn.execute(
        "UPDATE titles SET name = ?, info = ?, refreshed_at = ? WHERE id = ?", (name, json.dumps(info), now(), id)
    )


def delete_title(conn: sqlite3.Connection, id: int) -> bool:
    return conn.execute("DELETE FROM titles WHERE id = ?", (id,)).rowcount > 0


# --- Channels ---


def list_channels(conn: sqlite3.Connection) -> list[Channel]:
    rows = conn.execute("SELECT * FROM channels ORDER BY name COLLATE NOCASE")
    return [Channel.from_row(r) for r in rows]


def get_channel(conn: sqlite3.Connection, id: str) -> Channel | None:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (id,)).fetchone()
    return Channel.from_row(row) if row else None


def insert_channel(
    conn: sqlite3.Connection, id: str, name: str, handle: str | None, thumbnail: str | None
) -> bool:
    """False when it's already there."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO channels (id, name, handle, thumbnail, added_at) VALUES (?, ?, ?, ?, ?)",
        (id, name, handle, thumbnail, now()),
    )
    return cur.rowcount > 0


def delete_channel(conn: sqlite3.Connection, id: str) -> bool:
    conn.execute("DELETE FROM uploads WHERE channel_id = ?", (id,))
    return conn.execute("DELETE FROM channels WHERE id = ?", (id,)).rowcount > 0


# --- Uploads read last ---


def stored_uploads(conn: sqlite3.Connection) -> dict[str, tuple[list[Video], str | None, str]]:
    """(videos, error, fetched_at) by channel id."""
    rows = conn.execute("SELECT * FROM uploads")
    return {r["channel_id"]: ([Video(**v) for v in json.loads(r["videos"])], r["error"], r["fetched_at"]) for r in rows}


def save_uploads(conn: sqlite3.Connection, channel_id: str, videos: list[Video]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO uploads (channel_id, videos, error, fetched_at) VALUES (?, ?, NULL, ?)",
        (channel_id, json.dumps([asdict(v) for v in videos]), now()),
    )


def save_uploads_error(conn: sqlite3.Connection, channel_id: str, error: str) -> None:
    """The read failed: its uploads stay as they were (stale beats blank)."""
    conn.execute(
        "INSERT INTO uploads (channel_id, error, fetched_at) VALUES (?, ?, ?)"
        " ON CONFLICT (channel_id) DO UPDATE SET error = excluded.error, fetched_at = excluded.fetched_at",
        (channel_id, error, now()),
    )
