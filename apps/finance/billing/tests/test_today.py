"""The invoices ready to issue for lo9i's list of the user's day: those of the halves of a month that ended and aren't
issued, each needing issuing, nothing while the setup misses something, in the language asked."""

import asyncio
import json
from datetime import date

from conftest import NOW
from mcp import Client

from billing import db, today
from billing.mcp_server import mcp


def test_the_halves_that_ended_and_arent_issued(conn, issue):
    listed = today.listed(conn, NOW.date(), "en")
    assert listed["heading"] == "Invoices to issue"
    assert [(i["subtitle"], i["needs"]) for i in listed["items"]] == [
        ("1-15 Oct · $ 4,537,500.00", "Issue Acme S.A.'s invoice for 1-15 Oct"),
        ("16-31 Oct · $ 4,537,500.00", "Issue Acme S.A.'s invoice for 16-31 Oct"),
    ]
    first = listed["items"][0]
    assert (first["title"], first["kind"], first["key"]) == (
        "Acme S.A.",
        "invoices to issue",
        "billing acme 2026-10-01",
    )
    issue()
    assert [i["subtitle"][:9] for i in today.listed(conn, NOW.date(), "en")["items"]] == ["16-31 Oct"]
    later = today.listed(conn, date(2026, 11, 20), "es")["items"]
    assert [i["needs"] for i in later] == [
        "Emitir la factura de Acme S.A. del 16-31 oct",
        "Emitir la factura de Acme S.A. del 1-15 nov",
    ]
    assert later[0]["subtitle"] == "16-31 oct · $ 4.537.500,00"


def test_nothing_while_the_setup_misses_something():
    conn = db.connect(":memory:")
    assert today.listed(conn, NOW.date(), "en")["items"] == []


def test_the_tool_answers_the_list_as_json(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_DB", str(tmp_path / "billing.db"))

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool("today", {"day": "2026-11-03", "language": "en"})

    result = asyncio.run(run())
    assert not result.is_error and json.loads(result.content[0].text)["heading"] == "Invoices to issue"
