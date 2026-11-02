"""SQLite connection and schema."""

import fcntl
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
-- The issuer's setup, one row per setting (service.SETTINGS).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cuit TEXT NOT NULL UNIQUE,
    iva_condition TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    -- The invoice's line, followed by its period.
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- The monthly net amount from a date on, until a later one replaces it. '' is "from the start".
CREATE TABLE IF NOT EXISTS prices (
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    effective_from TEXT NOT NULL,
    monthly_net TEXT NOT NULL,
    PRIMARY KEY (customer_id, effective_from)
);

-- Amounts are decimal strings. draft: not sent to ARCA; sent: sent with `number`, answer unknown until
-- checked with ARCA; authorized: has its CAE; rejected: ARCA said no, nothing was issued.
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL CHECK (environment IN ('homologacion', 'produccion')),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    cbte_tipo INTEGER NOT NULL,
    point_of_sale INTEGER NOT NULL,
    number INTEGER,
    issue_date TEXT NOT NULL,
    period_from TEXT NOT NULL,
    period_to TEXT NOT NULL,
    due_date TEXT NOT NULL,
    description TEXT NOT NULL,
    net TEXT NOT NULL,
    iva TEXT NOT NULL,
    total TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'sent', 'authorized', 'rejected')),
    cae TEXT,
    cae_due TEXT,
    processed_at TEXT,
    observations TEXT NOT NULL DEFAULT '[]',
    errors TEXT NOT NULL DEFAULT '[]',
    -- The issuer and customer as they were when it was sent, for its PDF.
    issuer TEXT NOT NULL DEFAULT '{}',
    customer TEXT NOT NULL DEFAULT '{}',
    pdf_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One invoice per customer and period in each environment: a retry finds it instead of issuing another.
CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_period
    ON invoices (environment, customer_id, period_from, period_to);
CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_number
    ON invoices (environment, point_of_sale, cbte_tipo, number) WHERE number IS NOT NULL;

-- WSAA's tickets, kept until they expire: WSAA refuses a new one while the last is valid.
CREATE TABLE IF NOT EXISTS tickets (
    environment TEXT NOT NULL,
    service TEXT NOT NULL,
    -- The certificate's SHA-256: a ticket belongs to the certificate that signed in.
    certificate TEXT NOT NULL,
    token TEXT NOT NULL,
    sign TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (environment, service, certificate)
);
"""


class MissingDatabaseError(RuntimeError):
    """BILLING_DB isn't set."""


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the database (`path`, or BILLING_DB) and make sure the schema exists. There's no default: lo9i
    points BILLING_DB at the server's data folder, outside its code, which updates replace."""
    path = path or os.environ.get("BILLING_DB")
    if not path:
        raise MissingDatabaseError("BILLING_DB must point at the database file")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def invoices_dir() -> Path:
    """Where the PDFs go: BILLING_INVOICES, or an invoices folder next to the database."""
    if folder := os.environ.get("BILLING_INVOICES"):
        return Path(folder)
    if path := os.environ.get("BILLING_DB"):
        return Path(path).parent / "invoices"
    raise MissingDatabaseError("BILLING_DB must point at the database file")


_issuing = threading.Lock()


@contextmanager
def issuing(path: str | None = None) -> Iterator[None]:
    """One invoice issued at a time, by the MCP server or the page (two processes): issuing asks ARCA for the
    last number and then sends the next, and two at once would ask for the same."""
    path = path or os.environ.get("BILLING_DB")
    if not path:
        raise MissingDatabaseError("BILLING_DB must point at the database file")
    with _issuing, open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
