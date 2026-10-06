"""SQLite connection and schema."""

import os
import sqlite3

SCHEMA = """
-- Movies and TV shows, from TMDB.
CREATE TABLE IF NOT EXISTS titles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('movie', 'show')),
    tmdb_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'to_watch'
        CHECK (status IN ('to_watch', 'watching', 'watched', 'dropped')),
    -- Shows: the last episode watched, both unset before the first.
    season INTEGER,
    episode INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    -- What TMDB says about it (tmdb.py), as of refreshed_at.
    info TEXT NOT NULL DEFAULT '{}',
    refreshed_at TEXT NOT NULL,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, tmdb_id),
    CHECK ((season IS NULL) = (episode IS NULL))
);

-- YouTube channels. Their uploads are read from the channel's feed when asked
-- for, not stored.
CREATE TABLE IF NOT EXISTS channels (
    -- YouTube's channel id, UC...
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    handle TEXT,
    thumbnail TEXT,
    added_at TEXT NOT NULL
);
"""


class MissingDatabaseError(RuntimeError):
    """WATCHLIST_DB isn't set."""


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the database (`path`, or WATCHLIST_DB) and make sure the schema
    exists. There's no default: lo9i points WATCHLIST_DB at the server's data
    folder, outside its code, which updates replace. A connection may move
    between threads (the web page's handlers run in a thread pool), but is
    used by one at a time."""
    path = path or os.environ.get("WATCHLIST_DB")
    if not path:
        raise MissingDatabaseError("WATCHLIST_DB must point at the database file")
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
