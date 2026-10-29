"""The MCP tools: reading is marked read-only, so lo9i runs it without asking, and every change isn't, so it asks
first; Google's errors reach the model as tool errors."""

import json

from mcp import Client
from mcp.types import CallToolResult, TextContent

from lo9i_google_drive import mcp_server

READ = {"search_files", "read_file", "read_sheet"}
WRITE = {
    "create_document",
    "append_to_document",
    "replace_in_document",
    "rewrite_document",
    "create_spreadsheet",
    "write_cells",
    "append_rows",
    "clear_cells",
    "add_tab",
}


def _text(result: CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


async def _call(tool, /, **args):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(tool, args)
    return tools, result


async def test_reading_is_read_only_and_changes_ask_first(fake):
    fake.on("GET", "/drive/v3/files", {"files": []})
    tools, result = await _call("search_files", query="budget")
    assert set(tools) == READ | WRITE and not result.is_error
    read_only = {name for name, tool in tools.items() if tool.annotations and tool.annotations.read_only_hint}
    assert read_only == READ


async def test_not_signed_in_is_a_tool_error_that_says_how(fake, monkeypatch):
    monkeypatch.setenv("LO9I_OAUTH", "")
    _, result = await _call("read_file", file="1AbCdEfGhIjKlMnOpQrStUv")
    assert result.is_error and "Not signed in to Google yet" in _text(result)


async def test_rows_of_mixed_values_reach_the_sheet(fake):
    fake.on("PUT", "/v4/spreadsheets/1SheetIdForTheBudget0000/values/A1", {"updatedRange": "Sheet1!A1:C1"})
    _, result = await _call("write_cells", spreadsheet="1SheetIdForTheBudget0000", cells="A1", rows=[["x", 2, True]])
    assert not result.is_error and json.loads(_text(result))["updated_range"] == "Sheet1!A1:C1"
