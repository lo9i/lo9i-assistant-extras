import json

from mcp import Client

from lo9i_phone import contacts, dialer, mcp_server


async def _call(tool, /, **args):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(tool, args)
    return tools, result


async def test_find_contact_is_read_only_and_call_is_not(monkeypatch):
    monkeypatch.setattr(contacts, "search", lambda name: [contacts.Contact("Ana", numbers=[])])
    tools, result = await _call("find_contact", name="Ana")
    assert tools["find_contact"].annotations.read_only_hint
    assert not (tools["call"].annotations and tools["call"].annotations.read_only_hint)
    assert json.loads(result.content[0].text) == {"name": "Ana", "organization": "", "numbers": []}


async def test_call_says_whom_it_dialed_and_reports_errors(monkeypatch):
    async def dial(number):
        if number == "bad":
            raise dialer.DialError("'bad' isn't a phone number")
        return "+5491112345678"

    monkeypatch.setattr(dialer, "dial", dial)
    _, result = await _call("call", number="+54 9 11 1234-5678", name="Ana")
    assert result.content[0].text.startswith("The Phone app is open with Ana (+5491112345678)")
    _, result = await _call("call", number="bad")
    assert result.is_error and "isn't a phone number" in result.content[0].text


async def test_contacts_errors_reach_the_agent(monkeypatch):
    def search(name):
        raise contacts.ContactsError("No access to Contacts.")

    monkeypatch.setattr(contacts, "search", search)
    _, result = await _call("find_contact", name="Ana")
    assert result.is_error and "No access to Contacts." in result.content[0].text
