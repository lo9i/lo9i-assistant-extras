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


async def test_with_a_carddav_account_it_reads_that_instead_of_the_mac(monkeypatch):
    from lo9i_contacts import carddav

    searched = []

    async def search(self, query):
        searched.append((self._url, query))
        return [contacts.Contact("Ana")]

    monkeypatch.setattr(carddav.CardDav, "search", search)
    monkeypatch.setattr(contacts, "search", lambda query: [])
    for key, value in {"URL": "https://contacts.icloud.com", "USER": "me@icloud.com", "PASSWORD": "pw"}.items():
        monkeypatch.setenv(f"CARDDAV_{key}", value)
    mcp_server._account.cache_clear()
    _, result = await _call("find_contact", query="Ana")
    assert json.loads(result.content[0].text)["name"] == "Ana"
    assert searched == [("https://contacts.icloud.com/", "Ana")]
    monkeypatch.setenv("CARDDAV_PASSWORD", "")
    mcp_server._account.cache_clear()
    _, result = await _call("find_contact", query="Ana")
    assert result.is_error and "all three" in result.content[0].text
    mcp_server._account.cache_clear()
