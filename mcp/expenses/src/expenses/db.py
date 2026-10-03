"""SQLite connection and schema."""

import os
import sqlite3

DEFAULT_CATEGORIES = ("utilities", "tax", "insurance", "transport", "housing", "other")

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('house', 'car', 'other')),
    notes TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS obligations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT REFERENCES assets(id),
    name TEXT NOT NULL,
    category TEXT NOT NULL REFERENCES categories(name),
    notes TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    due_day INTEGER CHECK (due_day BETWEEN 1 AND 31),
    every_months INTEGER NOT NULL DEFAULT 1 CHECK (every_months IN (1, 2, 3, 6, 12)),
    anchor_month INTEGER CHECK (anchor_month BETWEEN 1 AND 12),
    expected_amount REAL,
    created_at TEXT NOT NULL
);

-- One name per asset, and one per "no asset".
CREATE UNIQUE INDEX IF NOT EXISTS idx_obligations_name
    ON obligations (COALESCE(asset_id, ''), name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation_id INTEGER REFERENCES obligations(id),
    -- Set only on bills without an obligation. Obligation bills take both
    -- from the obligation (see bills_v).
    asset_id TEXT REFERENCES assets(id),
    category TEXT REFERENCES categories(name),
    -- Billing month, YYYY-MM. Obligation bills only.
    period TEXT,
    estimated INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid')),
    paid_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (obligation_id IS NULL AND category IS NOT NULL AND period IS NULL)
        OR (obligation_id IS NOT NULL AND asset_id IS NULL AND category IS NULL
            AND period IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_obligation_period
    ON bills (obligation_id, period) WHERE obligation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bills_due ON bills (due_date);

-- Bills with their effective asset and category.
DROP VIEW IF EXISTS bills_v;
CREATE VIEW bills_v AS
SELECT
    b.id, b.obligation_id,
    CASE WHEN b.obligation_id IS NULL THEN b.asset_id ELSE o.asset_id END AS asset_id,
    CASE WHEN b.obligation_id IS NULL THEN b.category ELSE o.category END AS category,
    b.period, b.estimated, b.provider, b.amount, b.due_date, b.status, b.paid_at,
    b.notes, b.created_at, b.updated_at
FROM bills b
LEFT JOIN obligations o ON o.id = b.obligation_id;
"""


class MissingDatabaseError(RuntimeError):
    """EXPENSES_DB isn't set."""


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the database (`path`, or EXPENSES_DB) and make sure the schema
    and default categories exist. There's no default: lo9i points EXPENSES_DB
    at the server's data folder, outside its code, which updates replace."""
    path = path or os.environ.get("EXPENSES_DB")
    if not path:
        raise MissingDatabaseError("EXPENSES_DB must point at the database file")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO categories (name, created_at) VALUES (?, datetime('now'))",
            [(c,) for c in DEFAULT_CATEGORIES],
        )
    return conn
