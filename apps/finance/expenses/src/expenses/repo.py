"""SQL only. Validation and rules live in `service`."""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .models import Asset, Bill, Obligation


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _update(conn: sqlite3.Connection, table: str, key: Any, fields: dict[str, Any]) -> None:
    """UPDATE the given columns. Column names come from code, never input."""
    if not fields:
        return
    values = [json.dumps(v) if k == "metadata" else v for k, v in fields.items()]
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values, key])


def _count(conn: sqlite3.Connection, sql: str, *args: Any) -> int:
    return conn.execute(sql, args).fetchone()[0]


# --- Assets ---


def list_assets(conn: sqlite3.Connection) -> list[Asset]:
    rows = conn.execute("SELECT * FROM assets ORDER BY name COLLATE NOCASE")
    return [Asset.from_row(r) for r in rows]


def get_asset(conn: sqlite3.Connection, id: str) -> Asset | None:
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
    return Asset.from_row(row) if row else None


def insert_asset(
    conn: sqlite3.Connection, id: str, name: str, kind: str, notes: str, metadata: dict[str, str]
) -> bool:
    """False when the id is already taken."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO assets (id, name, kind, notes, metadata, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (id, name, kind, notes, json.dumps(metadata), now()),
    )
    return cur.rowcount > 0


def update_asset(conn: sqlite3.Connection, id: str, fields: dict[str, Any]) -> None:
    _update(conn, "assets", id, fields)


def count_asset_bills(conn: sqlite3.Connection, id: str) -> int:
    return _count(conn, "SELECT COUNT(*) FROM bills_v WHERE asset_id = ?", id)


def delete_asset(conn: sqlite3.Connection, id: str) -> bool:
    """Deletes the asset's obligations too. Callers check for bills first."""
    conn.execute("DELETE FROM obligations WHERE asset_id = ?", (id,))
    return conn.execute("DELETE FROM assets WHERE id = ?", (id,)).rowcount > 0


# --- Categories ---


def list_categories(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM categories ORDER BY name")]


def insert_category(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO categories (name, created_at) VALUES (?, ?)", (name, now())
    )


def count_category_uses(conn: sqlite3.Connection, name: str) -> int:
    return _count(
        conn,
        "SELECT (SELECT COUNT(*) FROM obligations WHERE category = ?1)"
        " + (SELECT COUNT(*) FROM bills WHERE category = ?1)",
        name,
    )


def delete_category(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("DELETE FROM categories WHERE name = ?", (name,)).rowcount > 0


# --- Obligations ---


def list_obligations(
    conn: sqlite3.Connection, asset_id: str | None = None, category: str | None = None
) -> list[Obligation]:
    rows = conn.execute(
        "SELECT * FROM obligations"
        " WHERE (?1 IS NULL OR asset_id = ?1) AND (?2 IS NULL OR category = ?2)"
        " ORDER BY asset_id IS NULL, asset_id, name COLLATE NOCASE",
        (asset_id, category),
    )
    return [Obligation.from_row(r) for r in rows]


def get_obligation(conn: sqlite3.Connection, id: int) -> Obligation | None:
    row = conn.execute("SELECT * FROM obligations WHERE id = ?", (id,)).fetchone()
    return Obligation.from_row(row) if row else None


def insert_obligation(
    conn: sqlite3.Connection,
    *,
    asset_id: str | None,
    name: str,
    category: str,
    notes: str,
    metadata: dict[str, str],
    due_day: int | None,
    every_months: int,
    anchor_month: int | None,
    expected_amount: float | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO obligations (asset_id, name, category, notes, metadata, due_day,"
        " every_months, anchor_month, expected_amount, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            asset_id,
            name,
            category,
            notes,
            json.dumps(metadata),
            due_day,
            every_months,
            anchor_month,
            expected_amount,
            now(),
        ),
    )
    return cur.lastrowid


def update_obligation(conn: sqlite3.Connection, id: int, fields: dict[str, Any]) -> None:
    _update(conn, "obligations", id, fields)


def count_obligation_bills(conn: sqlite3.Connection, id: int) -> int:
    return _count(conn, "SELECT COUNT(*) FROM bills WHERE obligation_id = ?", id)


def delete_obligation(conn: sqlite3.Connection, id: int) -> bool:
    return conn.execute("DELETE FROM obligations WHERE id = ?", (id,)).rowcount > 0


# --- Bills ---


def list_bills(
    conn: sqlite3.Connection,
    asset_id: str | None = None,
    category: str | None = None,
    status: str | None = None,
) -> list[Bill]:
    """Ordered by due date, earliest first. All filters are optional."""
    rows = conn.execute(
        "SELECT * FROM bills_v"
        " WHERE (?1 IS NULL OR asset_id = ?1) AND (?2 IS NULL OR category = ?2)"
        "   AND (?3 IS NULL OR status = ?3)"
        " ORDER BY due_date, id",
        (asset_id, category, status),
    )
    return [Bill.from_row(r) for r in rows]


def get_bill(conn: sqlite3.Connection, id: int) -> Bill | None:
    row = conn.execute("SELECT * FROM bills_v WHERE id = ?", (id,)).fetchone()
    return Bill.from_row(row) if row else None


def find_period_bill(conn: sqlite3.Connection, obligation_id: int, period: str) -> Bill | None:
    row = conn.execute(
        "SELECT * FROM bills_v WHERE obligation_id = ? AND period = ?", (obligation_id, period)
    ).fetchone()
    return Bill.from_row(row) if row else None


def last_confirmed_amount(
    conn: sqlite3.Connection, obligation_id: int, before_period: str
) -> float | None:
    """Amount of the obligation's latest non-estimated bill before a period."""
    row = conn.execute(
        "SELECT amount FROM bills"
        " WHERE obligation_id = ? AND estimated = 0 AND period < ?"
        " ORDER BY period DESC, id DESC LIMIT 1",
        (obligation_id, before_period),
    ).fetchone()
    return row[0] if row else None


def insert_bill(
    conn: sqlite3.Connection,
    *,
    amount: float,
    due_date: str,
    obligation_id: int | None = None,
    asset_id: str | None = None,
    category: str | None = None,
    period: str | None = None,
    estimated: bool = False,
    provider: str = "",
    notes: str = "",
) -> int:
    ts = now()
    cur = conn.execute(
        "INSERT INTO bills (obligation_id, asset_id, category, period, estimated, provider,"
        " amount, due_date, notes, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            obligation_id,
            asset_id,
            category,
            period,
            estimated,
            provider,
            amount,
            due_date,
            notes,
            ts,
            ts,
        ),
    )
    return cur.lastrowid


def update_bill(conn: sqlite3.Connection, id: int, fields: dict[str, Any]) -> None:
    if fields:
        _update(conn, "bills", id, {**fields, "updated_at": now()})


def delete_bill(conn: sqlite3.Connection, id: int) -> bool:
    return conn.execute("DELETE FROM bills WHERE id = ?", (id,)).rowcount > 0
