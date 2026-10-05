"""MCP server over stdio: find a contact's number and call it with the Mac's Phone app. lo9i starts it
(see server.yaml)."""

import asyncio
from dataclasses import asdict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lo9i_phone import contacts, dialer

INSTRUCTIONS = """\
Calls people with the Phone app on the user's Mac, through their iPhone. Find the number with
find_contact unless the user gave it, and when a contact has several numbers or several contacts
match, ask which one before calling. call only opens the Phone app: macOS asks the user to confirm,
and the user talks; you can't hear or speak on the call.
"""

mcp = MCPServer("phone", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True)


@mcp.tool(annotations=READ_ONLY)
async def find_contact(name: str) -> list[dict]:
    """Contacts on the user's Mac whose name matches, with their phone numbers and labels (mobile, home…).
    The first search asks the user, through macOS, to allow access to Contacts."""
    try:
        found = await asyncio.to_thread(contacts.search, name)
    except contacts.ContactsError as e:
        raise ToolError(str(e)) from None
    return [asdict(c) for c in found]


@mcp.tool()
async def call(number: str, name: str = "") -> str:
    """Call a phone number with the Phone app on the user's Mac, through their iPhone. `name` is who it is,
    for your reply. When a contact has several numbers or several contacts match, ask which one first.
    macOS asks the user to confirm before the call starts, and the user talks: you can't hear or speak on it."""
    try:
        dialed = await dialer.dial(number)
    except dialer.DialError as e:
        raise ToolError(str(e)) from None
    who = f"{name} ({dialed})" if name else dialed
    return f"The Phone app is open with {who}: the user confirms the call on their Mac, then talks."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
