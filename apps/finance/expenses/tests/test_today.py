"""The bills coming due for lo9i's list of the user's day: unpaid ones overdue or due within a week, the oldest first,
those due within three days or overdue saying they need paying, in the language asked."""

import asyncio
import json
from datetime import date

from mcp import Client

from expenses import service, today
from expenses.mcp_server import mcp

DAY = date(2026, 11, 3)


def _bill(conn, amount: float, due: str, provider: str, status: str = "pending") -> None:
    bill = service.add_bill(conn, amount, due, asset_id="h", category="utilities", provider=provider)
    if status == "paid":
        service.update_bill(conn, bill.id, status="paid")


def test_the_bills_due_this_week_and_those_to_pay_now(conn):
    _bill(conn, 61000, "2026-11-05", "Luz")
    _bill(conn, 900, "2026-10-28", "Gas")
    _bill(conn, 100, "2026-11-09", "Water")
    _bill(conn, 5, "2026-11-20", "Later")
    _bill(conn, 7, "2026-11-04", "Paid", status="paid")
    listed = today.listed(conn, DAY, "en")
    assert listed["heading"] == "Bills" and listed["note"] == ""
    assert [(i["title"], i["date"]) for i in listed["items"]] == [
        ("Gas", "2026-10-28"),
        ("Luz", "2026-11-05"),
        ("Water", "2026-11-09"),
    ]
    gas, luz, water = listed["items"]
    assert gas["subtitle"].endswith("House · overdue") and gas["needs"].startswith("Pay Gas (")
    assert gas["needs"].endswith("overdue since Oct 28")
    assert luz["needs"].endswith("due Nov 5") and water["needs"] == ""
    assert (luz["kind"], luz["key"], luz["page_path"]) == (
        "bill due",
        "expenses bill 1",
        "bills/1",
    )


def test_in_spanish_and_with_nothing_due(conn):
    _bill(conn, 61000, "2026-11-05", "Luz")
    [luz] = today.listed(conn, DAY, "es")["items"]
    assert luz["needs"].startswith("Pagar Luz (") and luz["needs"].endswith("vence el 5 de noviembre")
    assert today.listed(conn, date(2027, 1, 1), "es")["items"][0]["subtitle"].endswith("vencida")
    empty = today.listed(conn, date(2026, 1, 1), "es")
    assert empty["items"] == [] and empty["note"] == "No vencen facturas esta semana."


def test_the_tool_answers_the_list_as_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPENSES_DB", str(tmp_path / "test.db"))

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool("today", {"day": "2026-11-03", "language": "en"})

    result = asyncio.run(run())
    assert not result.is_error and json.loads(result.content[0].text)["heading"] == "Bills"


def test_the_badge_counts_the_unpaid_bills_due_that_day(conn):
    _bill(conn, 61000, "2026-11-03", "Luz")
    _bill(conn, 900, "2026-11-03", "Gas")
    _bill(conn, 7, "2026-11-03", "Paid", status="paid")
    _bill(conn, 100, "2026-11-02", "Overdue")
    _bill(conn, 5, "2026-11-04", "Tomorrow")
    assert today.due_today(conn, DAY, "en") == {"count": 2, "label": "2 bills due today"}
    assert today.due_today(conn, date(2026, 11, 4), "es") == {"count": 1, "label": "1 factura vence hoy"}
    assert today.due_today(conn, date(2026, 11, 5), "en")["count"] == 0


def test_the_badge_tool_answers_the_count_as_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPENSES_DB", str(tmp_path / "test.db"))

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool("badge", {"day": "2026-11-03", "language": "en"})

    result = asyncio.run(run())
    assert not result.is_error and json.loads(result.content[0].text) == {"count": 0, "label": "0 bills due today"}
