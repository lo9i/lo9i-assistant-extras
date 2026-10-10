from mcp import Client

from lo9i_phone import dialer, mcp_server


async def _call(tool, /, **args):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(tool, args)
    return tools, result


async def test_call_is_its_only_tool_and_isnt_read_only():
    async with Client(mcp_server.mcp) as client:
        tools = (await client.list_tools()).tools
    assert [t.name for t in tools] == ["call"]
    assert not (tools[0].annotations and tools[0].annotations.read_only_hint)


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
