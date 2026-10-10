import json

from mcp import Client

from lo9i_contacts import contacts, mcp_server


async def _call(tool, /, **args):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(tool, args)
    return tools, result


async def test_find_contact_is_read_only_and_returns_the_contacts(monkeypatch):
    monkeypatch.setattr(contacts, "search", lambda query: [contacts.Contact("Ana", birthday="--03-07")])
    tools, result = await _call("find_contact", query="Ana")
    assert tools["find_contact"].annotations.read_only_hint
    assert json.loads(result.content[0].text) == {
        "name": "Ana",
        "organization": "",
        "numbers": [],
        "emails": [],
        "birthday": "--03-07",
        "addresses": [],
    }


async def test_contacts_errors_reach_the_agent(monkeypatch):
    def search(query):
        raise contacts.ContactsError("No access to Contacts.")

    monkeypatch.setattr(contacts, "search", search)
    _, result = await _call("find_contact", query="Ana")
    assert result.is_error and "No access to Contacts." in result.content[0].text
