import asyncio
import json

import pytest
from mcp import Client

from expenses.mcp_server import mcp


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPENSES_DB", str(tmp_path / "test.db"))


def call(*calls: tuple[str, dict]) -> list:
    """Run tool calls in order over one in-process session."""

    async def run():
        async with Client(mcp) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return asyncio.run(run())


def payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_full_cycle():
    asset, ob, bill, paid, summary = call(
        ("add_asset", {"name": "Casa Boni", "kind": "house"}),
        ("add_obligation", {"name": "Luz", "category": "utilities", "asset_id": "casa-boni"}),
        ("add_bill", {"amount": 61000, "due_date": "2099-01-10", "obligation_id": 1}),
        ("update_bill", {"id": 1, "status": "paid"}),
        ("list_bills", {"status": "paid"}),
    )
    assert payload(asset)["id"] == "casa-boni"
    assert payload(ob)["asset_id"] == "casa-boni"
    assert payload(bill)["period"] == "2099-01"
    assert payload(paid)["paid_at"] is not None
    assert summary.structured_content["result"][0]["amount"] == 61000


def test_service_errors_reach_the_model():
    (result,) = call(("add_bill", {"amount": 10, "due_date": "2026-10-01", "asset_id": "x"}))
    assert result.is_error
    assert "category is required" in result.content[0].text


def test_clear_unsets_a_field():
    _, cleared, bad = call(
        ("add_obligation", {"name": "Gas", "category": "utilities", "expected_amount": 5}),
        ("update_obligation", {"id": 1, "clear": ["expected_amount"]}),
        ("update_obligation", {"id": 1, "clear": ["name"]}),
    )
    assert payload(cleared)["expected_amount"] is None
    assert bad.is_error and "clearable fields" in bad.content[0].text


def test_month_summary_totals():
    (_, _, _, summary) = call(
        ("add_bill", {"amount": 100, "due_date": "2026-01-05", "category": "tax"}),
        ("add_bill", {"amount": 50.5, "due_date": "2026-01-06", "category": "other"}),
        ("update_bill", {"id": 2, "status": "paid"}),
        ("month_summary", {}),
    )
    s = payload(summary)
    # Both are earlier months; only the unpaid one is still listed.
    assert [b["id"] for b in s["bills"]] == [1]
    assert (s["total"], s["paid"], s["pending"]) == (100, 0, 100)
    assert s["by_category"] == {"tax": 100}


def test_only_read_tools_are_read_only():
    async def run():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    read_only = {t.name for t in asyncio.run(run()) if t.annotations and t.annotations.read_only_hint}
    assert read_only == {
        "month_summary",
        "list_assets",
        "list_categories",
        "list_obligations",
        "list_bills",
        "get_bill",
    }


def test_the_currency_is_chosen_once_and_reported():
    before, chosen, bad, after = call(
        ("month_summary", {}),
        ("set_currency", {"currency_code": "eur", "locale": "de_DE"}),
        ("set_currency", {"currency_code": "pesos"}),
        ("month_summary", {}),
    )
    assert payload(before)["currency"] is None and "set_currency" in payload(before)["note"]
    assert payload(chosen) == {"currency": "EUR", "locale": "de_DE", "example": "45.230,50\xa0€"}
    assert bad.is_error and "ISO 4217" in bad.content[0].text
    assert payload(after)["currency"] == "EUR"
