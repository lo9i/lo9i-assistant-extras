"""MCP server over stdio: the user's Google Drive, Docs and Sheets. lo9i starts it (see server.yaml) with the user's
Google sign-in in LO9I_OAUTH, once they've signed in in the plugin's setup."""

from collections.abc import Awaitable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from lo9i_google_drive import drive
from lo9i_google_drive.drive import Cell
from lo9i_google_drive.google import GoogleError

INSTRUCTIONS = """\
The user's Google Drive, Docs and Sheets, as the Google account they signed in with. Find files with search_files,
then name them by the id or link it gives (a link the user pastes works too). Read any file with read_file (a Doc
as Markdown, a Sheet's tabs as rows, Slides and PDFs as text) and a spreadsheet's cells with read_sheet. Creating
and changing Docs and Sheets asks the user for approval unless they trust the plugin; give them the link after.
"""

mcp = MCPServer("google-drive", instructions=INSTRUCTIONS)

# Lets clients run these without approval.
READ_ONLY = ToolAnnotations(read_only_hint=True)
# Changes the user's files: asked first, unless the user trusts the plugin.
CHANGES = ToolAnnotations(read_only_hint=False, destructive_hint=False)
REWRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True)

File = Annotated[str, Field(description="The file's id or link, from search_files or the user")]
Doc = Annotated[str, Field(description="The Google Doc's id or link")]
Sheet = Annotated[str, Field(description="The Google Sheet's id or link")]
Folder = Annotated[str, Field(description="A folder's id or link to put it in; empty for My Drive")]
Cells = Annotated[str, Field(description="Cells in A1 notation, with the tab's name: 'Budget'!A1:D20")]
Rows = Annotated[list[list[Cell]], Field(description="Rows of cell values, as typed: text, numbers, =formulas")]


async def _run[T](call: Awaitable[T]) -> T:
    """Google's and the sign-in's errors reach the model as tool errors it can act on."""
    try:
        return await call
    except GoogleError as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=READ_ONLY)
async def search_files(
    query: Annotated[str, Field(description="Words in the file's name or text; empty for the latest changed")] = "",
    kind: Annotated[
        str, Field(description="Only this kind: document, spreadsheet, presentation, folder, pdf, form, drawing")
    ] = "",
    folder: Annotated[str, Field(description="Only files in this folder (an id or link, or root for My Drive)")] = "",
    modified_after: Annotated[str, Field(description="Only files changed after this time, like 2026-10-01")] = "",
    limit: Annotated[int, Field(description="How many, up to 50")] = 20,
) -> dict[str, Any]:
    """Find the user's files in Google Drive (theirs, shared with them, and shared drives'), by words in their name or
    text, kind, folder or date. Each comes with its id, kind, when it last changed, its owner and its link. Lists a
    folder's files with `folder` alone."""
    return await _run(drive.search_files(query, kind, folder, modified_after, limit))


@mcp.tool(annotations=READ_ONLY)
async def read_file(
    file: File,
    start: Annotated[int, Field(description="The character to read on from, for a long file")] = 0,
) -> dict[str, Any]:
    """Read a file in Google Drive: a Google Doc as Markdown, a Google Sheet's tabs as rows, Google Slides, a PDF's
    text, or a text file. Long text comes in parts: `more` says where the next starts. A folder lists its files."""
    return await _run(drive.read_file(file, start))


@mcp.tool(annotations=READ_ONLY)
async def read_sheet(
    spreadsheet: Sheet,
    cells: Annotated[str, Field(description="Cells in A1 notation, like 'Budget'!A1:D20; empty for every tab")] = "",
) -> dict[str, Any]:
    """Read a Google Sheet: its tabs with their size, and the values (as shown) in `cells`, or in each tab's first
    500 rows."""
    return await _run(drive.read_sheet(spreadsheet, cells))


@mcp.tool(annotations=CHANGES)
async def create_document(
    title: str,
    content: Annotated[str, Field(description="The text, in Markdown: headings, lists, tables, links, bold")] = "",
    folder: Folder = "",
) -> dict[str, Any]:
    """Create a Google Doc, with `content` written in Markdown, which becomes the Doc's formatting."""
    return await _run(drive.create_document(title, content, folder))


@mcp.tool(annotations=CHANGES)
async def append_to_document(
    document: Doc, text: Annotated[str, Field(description="Plain text to add")]
) -> dict[str, Any]:
    """Add text at the end of a Google Doc, as new paragraphs (one per line)."""
    return await _run(drive.append_to_document(document, text))


@mcp.tool(annotations=REWRITES)
async def replace_in_document(
    document: Doc,
    find: Annotated[str, Field(description="The exact text to find")],
    replace: Annotated[str, Field(description="What it becomes; empty to delete it")],
    match_case: bool = True,
) -> dict[str, Any]:
    """Replace every occurrence of a text in a Google Doc, keeping its formatting. Says how many it changed: read
    the Doc first to find the exact text."""
    return await _run(drive.replace_in_document(document, find, replace, match_case))


@mcp.tool(annotations=REWRITES)
async def rewrite_document(
    document: Doc,
    content: Annotated[str, Field(description="The Doc's whole new text, in Markdown")],
) -> dict[str, Any]:
    """Replace a Google Doc's whole content with `content` (Markdown), keeping its name, folder, sharing and version
    history. For a big change; for a few words use replace_in_document. Read it first and keep what should stay."""
    return await _run(drive.rewrite_document(document, content))


@mcp.tool(annotations=CHANGES)
async def create_spreadsheet(
    title: str,
    rows: Annotated[list[list[Cell]] | None, Field(description="Rows to start it with, from A1, as typed")] = None,
    tab: Annotated[str, Field(description="The first tab's name; empty for Sheet1")] = "",
    folder: Folder = "",
) -> dict[str, Any]:
    """Create a Google Sheet, optionally with rows (a header row first) in its first tab."""
    return await _run(drive.create_spreadsheet(title, rows, tab, folder))


@mcp.tool(annotations=REWRITES)
async def write_cells(spreadsheet: Sheet, cells: Cells, rows: Rows) -> dict[str, Any]:
    """Write rows of values into a Google Sheet from the top left cell of `cells`, over what's there, as if typed:
    =formulas work, and numbers and dates are read as such."""
    return await _run(drive.write_cells(spreadsheet, cells, rows))


@mcp.tool(annotations=CHANGES)
async def append_rows(
    spreadsheet: Sheet,
    rows: Rows,
    tab: Annotated[str, Field(description="The tab's name; empty for the first tab")] = "",
) -> dict[str, Any]:
    """Add rows under the last row of a tab's table in a Google Sheet, as if typed."""
    return await _run(drive.append_rows(spreadsheet, rows, tab))


@mcp.tool(annotations=REWRITES)
async def clear_cells(spreadsheet: Sheet, cells: Cells) -> dict[str, Any]:
    """Empty the values in a range of a Google Sheet; their formatting stays."""
    return await _run(drive.clear_cells(spreadsheet, cells))


@mcp.tool(annotations=CHANGES)
async def add_tab(spreadsheet: Sheet, title: str) -> dict[str, Any]:
    """Add a tab at the end of a Google Sheet."""
    return await _run(drive.add_tab(spreadsheet, title))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
