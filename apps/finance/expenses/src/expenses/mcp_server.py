"""MCP server over the service layer, over stdio. lo9i starts it (see
server.yaml) with EXPENSES_DB set:

    EXPENSES_DB=/path/expenses.db expenses-mcp
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import currency, db, recurring, service
from .service import UNSET

INSTRUCTIONS = """\
Tracks expenses, all in one currency the user chose. month_summary says
which (currency and locale) and amounts are plain numbers in it. If it says
none is chosen yet, ask the user and call set_currency.

- Assets are things owned that generate expenses: houses, cars (kind house,
  car or other). Ids are slugs, e.g. "boni".
- Obligations are things paid repeatedly (Luz, ABL, Patente, Monotributo).
  They may belong to an asset or stand alone (taxes). With a due_day, a
  pending bill is created ahead of time for each scheduled month, its amount
  fixed (expected_amount) or estimated from the last real bill.
- Bills are single payments. Adding a bill for an obligation and month that
  already has one updates that bill: this is how a real bill replaces the
  estimate. One-off bills need a category instead of an obligation.
- Categories are created on first use; prefer an existing one
  (list_categories).

Start with month_summary to see what is due. Errors list the valid choices.
"""

mcp = MCPServer("expenses", instructions=INSTRUCTIONS)

# Lets clients run these without approval. They still create due recurring
# bills first (see _db), which is upkeep, not a change the caller asked for.
READ_ONLY = ToolAnnotations(read_only_hint=True)


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """A connection per call, with this month's recurring bills in place.
    Service errors reach the model as tool errors it can act on."""
    conn = db.connect()
    try:
        recurring.generate(conn)
        yield conn
    except service.ExpensesError as e:
        raise ToolError(f"{type(e).__name__}: {e}") from None
    finally:
        conn.close()


def _changes(clear: list[str] | None, clearable: set[str], **given: Any) -> dict[str, Any]:
    """Keyword arguments for an update: given values, plus None for fields
    named in `clear`. Absent fields are left out, so they stay as they are."""
    out = {k: v for k, v in given.items() if v is not None}
    for f in clear or []:
        if f not in clearable:
            raise ToolError(f"cannot clear {f!r}, clearable fields: {sorted(clearable)}")
        out[f] = None
    return out


def _bills(bills: list) -> list[dict]:
    return [asdict(b) for b in bills]


# --- Overview ---


@mcp.tool(annotations=READ_ONLY)
def month_summary() -> dict:
    """This month's bills plus unpaid ones from earlier months, with totals
    overall, by category and by asset."""
    with _db() as conn:
        bills = service.month_bills(conn)
        chosen = currency.get(conn)
    return {
        "month": date.today().strftime("%Y-%m"),
        **_currency(chosen),
        **service.totals(bills),
        "bills": _bills(bills),
    }


def _currency(chosen: currency.Currency | None) -> dict:
    if chosen is None:
        return {"currency": None, "note": "No currency chosen yet: ask the user, then call set_currency."}
    return {"currency": chosen.code, "locale": chosen.locale, "example": chosen.format(45230.5)}


@mcp.tool()
def set_currency(
    currency_code: Annotated[str, Field(description="ISO 4217, like ARS, USD or EUR")],
    locale: Annotated[
        str | None, Field(description="How numbers are written, like es_AR or en_US; absent keeps the current one")
    ] = None,
) -> dict:
    """Choose the one currency every amount is in. Changing it relabels
    existing amounts; nothing is converted."""
    with _db() as conn:
        return _currency(currency.set(conn, currency_code, locale))


# --- Assets ---


@mcp.tool(annotations=READ_ONLY)
def list_assets() -> list[dict]:
    """All assets (houses, cars...), ordered by name."""
    with _db() as conn:
        return [asdict(a) for a in service.list_assets(conn)]


@mcp.tool()
def add_asset(
    name: str,
    kind: Annotated[str, Field(description="house, car or other")] = "other",
    id: Annotated[str | None, Field(description="Slug id; derived from name when absent")] = None,
    notes: str = "",
    metadata: Annotated[
        dict[str, str] | None, Field(description='Free-form facts, e.g. {"address": "..."}')
    ] = None,
) -> dict:
    """Create an asset."""
    with _db() as conn:
        return asdict(service.add_asset(conn, name, kind, id=id, notes=notes, metadata=metadata))


@mcp.tool()
def update_asset(
    id: str,
    name: str | None = None,
    kind: str | None = None,
    notes: str | None = None,
    metadata: Annotated[
        dict[str, str] | None, Field(description="Replaces the stored object whole")
    ] = None,
) -> dict:
    """Change an asset. Omitted fields stay as they are."""
    with _db() as conn:
        changes = _changes(None, set(), name=name, kind=kind, notes=notes, metadata=metadata)
        return asdict(service.update_asset(conn, id, **changes))


@mcp.tool()
def remove_asset(id: str) -> str:
    """Delete an asset and its obligations. Refused while it has bills."""
    with _db() as conn:
        service.remove_asset(conn, id)
    return f"removed asset {id}"


# --- Categories ---


@mcp.tool(annotations=READ_ONLY)
def list_categories() -> list[str]:
    """All categories."""
    with _db() as conn:
        return service.list_categories(conn)


@mcp.tool()
def add_category(name: str) -> str:
    """Create a category. Idempotent; categories are also created on first use."""
    with _db() as conn:
        return service.add_category(conn, name)


@mcp.tool()
def remove_category(name: str) -> str:
    """Delete a category. Refused while obligations or bills use it."""
    with _db() as conn:
        service.remove_category(conn, name)
    return f"removed category {name}"


# --- Obligations ---


@mcp.tool(annotations=READ_ONLY)
def list_obligations(asset_id: str | None = None, category: str | None = None) -> list[dict]:
    """Obligations, optionally filtered by asset or category."""
    with _db() as conn:
        return [asdict(o) for o in service.list_obligations(conn, asset_id, category)]


@mcp.tool()
def add_obligation(
    name: str,
    category: str,
    asset_id: Annotated[str | None, Field(description="Omit for obligations of no asset")] = None,
    due_day: Annotated[
        int | None, Field(description="1-31. Set = bills are created ahead of time")
    ] = None,
    every_months: Annotated[int, Field(description="1, 2, 3, 6 or 12")] = 1,
    anchor_month: Annotated[
        int | None,
        Field(description="1-12, a month the bill falls on. Required when every_months > 1"),
    ] = None,
    expected_amount: Annotated[
        float | None, Field(description="Fixed amount; omit to estimate from the last bill")
    ] = None,
    notes: str = "",
    metadata: Annotated[
        dict[str, str] | None, Field(description='e.g. {"client_number": "0012345"}')
    ] = None,
) -> dict:
    """Create an obligation."""
    with _db() as conn:
        return asdict(
            service.add_obligation(
                conn,
                name,
                category,
                asset_id=asset_id,
                notes=notes,
                metadata=metadata,
                due_day=due_day,
                every_months=every_months,
                anchor_month=anchor_month,
                expected_amount=expected_amount,
            )
        )


@mcp.tool()
def update_obligation(
    id: int,
    name: str | None = None,
    category: str | None = None,
    due_day: int | None = None,
    every_months: int | None = None,
    anchor_month: int | None = None,
    expected_amount: float | None = None,
    notes: str | None = None,
    metadata: Annotated[
        dict[str, str] | None, Field(description="Replaces the stored object whole")
    ] = None,
    clear: Annotated[
        list[str] | None,
        Field(
            description="Fields to unset: due_day (stop repeating), expected_amount "
            "(estimate instead), anchor_month"
        ),
    ] = None,
) -> dict:
    """Change an obligation. Omitted fields stay as they are."""
    with _db() as conn:
        changes = _changes(
            clear,
            {"due_day", "expected_amount", "anchor_month"},
            name=name,
            category=category,
            due_day=due_day,
            every_months=every_months,
            anchor_month=anchor_month,
            expected_amount=expected_amount,
            notes=notes,
            metadata=metadata,
        )
        return asdict(service.update_obligation(conn, id, **changes))


@mcp.tool()
def remove_obligation(id: int) -> str:
    """Delete an obligation. Refused while it has bills."""
    with _db() as conn:
        service.remove_obligation(conn, id)
    return f"removed obligation {id}"


# --- Bills ---


@mcp.tool(annotations=READ_ONLY)
def list_bills(
    asset_id: str | None = None,
    category: str | None = None,
    status: Annotated[str | None, Field(description="pending or paid")] = None,
) -> list[dict]:
    """Bills ordered by due date, optionally filtered."""
    with _db() as conn:
        return _bills(service.list_bills(conn, asset_id, category, status))


@mcp.tool(annotations=READ_ONLY)
def get_bill(id: int) -> dict:
    """One bill."""
    with _db() as conn:
        return asdict(service.get_bill(conn, id))


@mcp.tool()
def add_bill(
    amount: float,
    due_date: Annotated[str, Field(description="YYYY-MM-DD")],
    obligation_id: Annotated[
        int | None,
        Field(description="If its month already has a bill, that bill is updated instead"),
    ] = None,
    category: Annotated[
        str | None, Field(description="Required without an obligation")
    ] = None,
    asset_id: str | None = None,
    period: Annotated[
        str | None,
        Field(description="YYYY-MM billing month, obligation bills only. Defaults to due_date's"),
    ] = None,
    provider: str = "",
    notes: str = "",
) -> dict:
    """Record a bill, for an obligation or as a one-off."""
    with _db() as conn:
        return asdict(
            service.add_bill(
                conn,
                amount,
                due_date,
                obligation_id=obligation_id,
                asset_id=asset_id,
                category=category,
                period=period,
                provider=provider,
                notes=notes,
            )
        )


@mcp.tool()
def update_bill(
    id: int,
    amount: float | None = None,
    due_date: str | None = None,
    status: Annotated[str | None, Field(description="pending or paid")] = None,
    obligation_id: int | None = None,
    asset_id: str | None = None,
    category: str | None = None,
    period: str | None = None,
    provider: str | None = None,
    notes: str | None = None,
    clear: Annotated[
        list[str] | None,
        Field(
            description="Fields to unset: obligation_id (make it a one-off), "
            "asset_id (one-offs only)"
        ),
    ] = None,
) -> dict:
    """Change a bill, e.g. status="paid". Omitted fields stay as they are."""
    with _db() as conn:
        changes = _changes(
            clear,
            {"obligation_id", "asset_id"},
            amount=amount,
            due_date=due_date,
            status=status,
            obligation_id=obligation_id,
            asset_id=asset_id,
            category=category,
            period=period,
            provider=provider,
            notes=notes,
        )
        return asdict(service.update_bill(conn, id, **changes))


@mcp.tool()
def remove_bill(id: int) -> str:
    """Delete a bill."""
    with _db() as conn:
        service.remove_bill(conn, id)
    return f"removed bill {id}"


def main() -> None:
    # Fail now on a bad database path, not on the first tool call.
    db.connect().close()
    mcp.run()


if __name__ == "__main__":
    main()
