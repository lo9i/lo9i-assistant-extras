"""MCP server over stdio: call a number with the Mac's Phone app. lo9i starts it (see server.yaml). Numbers
come from the contacts plugin."""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from lo9i_phone import dialer

INSTRUCTIONS = """\
Calls people with the Phone app on the user's Mac, through their iPhone. Unless the user gave the
number, find it with the contacts plugin's find_contact, and when a contact has several numbers or
several contacts match, ask which one before calling. call only opens the Phone app: macOS asks the
user to confirm, and the user talks; you can't hear or speak on the call.
"""

mcp = MCPServer("phone", instructions=INSTRUCTIONS)


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
