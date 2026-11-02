import asyncio
import json
import os

import pytest
from conftest import make_certificate
from mcp import Client

from billing import mcp_server
from billing.mcp_server import mcp


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("BILLING_DB", str(tmp_path / "billing.db"))
    monkeypatch.setenv("BILLING_INVOICES", str(tmp_path / "invoices"))
    monkeypatch.setattr(mcp_server, "SEND", fake)


def call(*calls: tuple[str, dict]) -> list:
    """Run tool calls in order over one in-process session."""

    async def run():
        async with Client(mcp) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return asyncio.run(run())


def payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_set_up_add_a_customer_prepare_and_issue_a_month(tmp_path, fake):
    cert, key = make_certificate(tmp_path)
    shown, customer, preview, first, again, invoices = call(
        (
            "setup",
            {
                "cuit": "20-12345678-6",
                "business_name": "Ana Pérez Software",
                "address": "Av. Corrientes 1234, CABA",
                "iva_condition": "responsable_inscripto",
                "activity_start": "2015-03-01",
                "iibb": "901-123456-7",
                "point_of_sale": 2,
                "certificate_path": str(cert),
                "key_path": str(key),
            },
        ),
        ("add_customer", {"name": "Acme S.A.", "cuit": "30-71234567-1", "monthly_net": 7500000, "id": "acme"}),
        ("prepare_month", {"customer": "acme", "month": "2026-10"}),
        ("create_invoice", {"customer": "acme", "period_from": "2026-10-01", "period_to": "2026-10-15"}),
        ("create_invoice", {"customer": "30712345671", "period_from": "2026-10-01", "period_to": "2026-10-15"}),
        ("list_invoices", {"month": "2026-10"}),
    )
    assert payload(shown)["ready"] is True
    assert payload(customer)["monthly_net"] == 7500000
    assert [i["total"] for i in payload(preview)["invoices"]] == [4537500, 4537500]
    issued = payload(first)
    assert issued["status"] == "authorized" and os.path.isfile(issued["pdf_path"])
    assert payload(again)["already_issued"] is True and payload(again)["id"] == issued["id"]
    assert fake.calls.count("FECAESolicitar") == 1
    assert len(invoices.structured_content["result"]) == 1

    (got,) = call(("get_invoice", {"id": issued["id"]}))
    assert payload(got)["pdf_path"] == issued["pdf_path"]


def test_service_errors_reach_the_model():
    (result,) = call(("create_invoice", {"customer": "nobody", "period_from": "2026-10-01", "period_to": "2026-10-15"}))
    assert result.is_error
    assert "NotReadyError" in result.content[0].text and "setup needs cuit" in result.content[0].text


def test_only_reading_tools_are_read_only():
    async def run():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(run())
    read_only = {t.name for t in tools if t.annotations and t.annotations.read_only_hint}
    assert read_only == {"check_arca", "list_customers", "prepare_month", "list_invoices", "get_invoice"}
    assert {t.name for t in tools} - read_only == {"setup", "add_customer", "update_customer", "create_invoice"}
