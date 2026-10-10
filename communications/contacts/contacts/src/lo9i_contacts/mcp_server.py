"""MCP server over stdio: look people up in the Mac's Contacts. lo9i starts it (see server.yaml)."""

import asyncio
from dataclasses import asdict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lo9i_contacts import contacts

INSTRUCTIONS = """\
The people in the Contacts on the user's Mac. Look someone up with find_contact before asking the
user for a number, an email address, a birthday or an address. When several contacts match, ask which
one before acting on it.
"""

mcp = MCPServer("contacts", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True)


@mcp.tool(annotations=READ_ONLY)
async def find_contact(query: str) -> list[dict]:
    """Contacts on the user's Mac matching `query`: a name, an email address or a phone number. Each comes
    with its phone numbers, emails and addresses (labelled mobile, home, work…) and its birthday (YYYY-MM-DD,
    or --MM-DD without the year). The first search asks the user, through macOS, to allow access to Contacts."""
    try:
        found = await asyncio.to_thread(contacts.search, query)
    except contacts.ContactsError as e:
        raise ToolError(str(e)) from None
    return [asdict(c) for c in found]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
